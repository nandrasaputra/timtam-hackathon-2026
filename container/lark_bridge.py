#!/usr/bin/env python3
"""Lark @mention bridge. Runs on the host. Forwards one message to Cursor Agent, then replies in the thread."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import base64
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackCard,
    CallBackToast,
    P2CardActionTriggerResponse,
)

import mr_flow

CURSOR_AGENT_URL = os.environ.get("CURSOR_AGENT_URL", "http://127.0.0.1:4096")
_LINKED_RECORD_GUIDANCE = "Investigate the request below using the linked Lark record as context."
_VERSION_LINE = re.compile(r"(?im)^(?P<label>[^:\n]*version[^:\n]*):\s*(?P<value>.+)$")
_RC_BRANCH = re.compile(r"rc/[A-Za-z0-9._-]+", re.IGNORECASE)
_VERSION_NUMBER = re.compile(r"\d+\.\d+(?:\.\d+)?")
_TEXT_ONLY_EVIDENCE = (
    "The active model cannot read images in this Lark conversation. "
    "Treat image markers as unavailable evidence. Do not ask for screenshots, photos, "
    "or other images. If more evidence is essential, ask for one useful text equivalent, "
    "such as the exact error text, what the user sees, or the reproduction steps."
)
_PHONE = re.compile(r"\+62[\d\s-]{8,16}|\b08\d{8,13}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
_TOKEN = re.compile(r"\b(?:glpat|sk)-[A-Za-z0-9_-]+\b")
_COORD = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")
_ID = re.compile(
    r"\b(?:customer_id|driver_id|user_id|order_id|booking_id)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_NAME = re.compile(
    r"\b(?:customer_name|driver_name|user_name|full_name|name)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_ADDRESS = re.compile(r"\b(?:address|addr)\s*[:=]\s*\S+", re.IGNORECASE)
_DEVICE = re.compile(
    r"\b(?:device_id|android_id|advertising_id|idfa|gaid)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_MENTION = re.compile(r"@_user_\d+")
_LEADING_GREETING = re.compile(r"^([^\n:]{1,100}:[ \t]*)?(?:hi|hallo|hola|hello|hey)\b[ \t,.!]+(?=\S)", re.I)


def redact(text: str) -> str:
    text = _PHONE.sub("[PHONE]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _TOKEN.sub("[TOKEN]", text)
    text = _COORD.sub("[COORD]", text)
    text = _ID.sub("[ID]", text)
    text = _NAME.sub("[NAME]", text)
    text = _ADDRESS.sub("[ADDRESS]", text)
    text = _DEVICE.sub("[DEVICE]", text)
    return text


def show_mentions(text: str, names: dict) -> str:
    def repl(match):
        name = (names.get(match.group(0)) or "").strip()
        return "@" + (name or "someone")

    return _MENTION.sub(repl, text or "")


def clean(text: str) -> str:
    return redact(_MENTION.sub("", text or "")).strip()


class Tracker:
    def __init__(self):
        self.done = set()
        self._busy = set()
        self._lock = threading.Lock()

    def busy(self, message_id: str) -> bool:
        with self._lock:
            return message_id in self._busy

    def skip(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self.done or message_id in self._busy:
                return True
            self._busy.add(message_id)
            return False

    def release(self, message_id: str) -> None:
        with self._lock:
            self._busy.discard(message_id)

    def complete(self, message_id: str) -> None:
        with self._lock:
            self._busy.discard(message_id)
            self.done.add(message_id)


seen = Tracker()


def watch_channels() -> set[str]:
    return {
        chat_id
        for key in (
            "LARK_CHANNEL_HACK_OF_THRONES",
            "LARK_CHANNEL_FCT",
            "LARK_CHANNEL_CRASH",
            "LARK_CHANNEL_FEEDBACK",
        )
        if (chat_id := os.environ.get(key, ""))
    }


def event_key(data) -> str:
    header = getattr(data, "header", None)
    event_id = getattr(header, "event_id", "") or ""
    if event_id:
        return event_id
    return getattr(data.event.message, "message_id", "") or ""


def image_keys(node) -> list[str]:
    found = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for field in ("image_key", "img_key"):
                key = value.get(field)
                if isinstance(key, str) and key and key not in found:
                    found.append(key)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def sniff_mime(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw.startswith(b"GIF8"):
        return "image/gif"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def supports_image_input() -> bool:
    flag = os.environ.get("CURSOR_IMAGE_INPUT", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def image_part(raw: bytes):
    # ponytail: 1.5MB each, 4 images. A bigger QA dump is skipped. Upgrade path is a vision resize.
    mime = sniff_mime(raw)
    if not mime or len(raw) > 1_500_000:
        return None
    ext = "jpg" if mime == "image/jpeg" else mime.split("/")[1]
    encoded = base64.b64encode(raw).decode()
    return {"type": "file", "mime": mime, "filename": f"qa.{ext}", "url": f"data:{mime};base64,{encoded}"}


_MESSAGE_LINK = re.compile(
    r"https://applink\.(?:larksuite|feishu|larkoffice)\.com/client/message/link/open\?token=[^\s)]+"
)
_RECORD_LINK = re.compile(
    r"https://[A-Za-z0-9.-]+\.(?:larksuite|feishu|larkoffice)\.com/record/([A-Za-z0-9]+)(?:\?[^\s)]*)?"
)
_WEB_URL = re.compile(
    r"https://applink\.(?:larksuite|feishu|larkoffice)\.(?:com|cn)/client/web_url/open\?[^\s)]+"
)


def card_text(node) -> str:
    found = []

    def add(value: str) -> None:
        text = value.strip()
        if text and text not in found:
            found.append(text)

    def walk(obj) -> None:
        if isinstance(obj, list):
            for item in obj:
                walk(item)
            return
        if not isinstance(obj, dict):
            return
        if obj.get("tag") == "at":
            add("@" + ((obj.get("user_name") or "").strip() or "someone"))
        for key in ("text", "content", "placeholder", "title"):
            val = obj.get(key)
            if isinstance(val, str):
                add(val)
        href = obj.get("href")
        if isinstance(href, str):
            add(href)
        for val in obj.values():
            if isinstance(val, (dict, list)):
                walk(val)

    walk(node)
    return "\n".join(found)


def text_from_body(parsed: dict) -> str:
    if any(key in parsed for key in ("elements", "header", "body", "title")):
        extra = card_text(parsed)
        if extra:
            return extra
    if parsed.get("text"):
        return parsed["text"]
    parts = []
    for row in parsed.get("content") or []:
        if not isinstance(row, list):
            continue
        for node in row:
            if not isinstance(node, dict):
                continue
            if node.get("tag") == "text" and node.get("text"):
                parts.append(node["text"])
            elif node.get("tag") == "at":
                parts.append("@" + ((node.get("user_name") or "").strip() or "someone"))
            elif node.get("tag") == "img":
                parts.append("[image]")
    return " ".join(parts)


def item_from(message) -> dict:
    if isinstance(message, dict):
        return message
    content = getattr(message, "content", None)
    body = getattr(message, "body", None)
    if content is None and body is not None:
        content = getattr(body, "content", "") or ""
    try:
        parsed = json.loads(content) if content else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    sender = getattr(message, "sender", None)
    mentions = {}
    for mention in getattr(message, "mentions", None) or []:
        key = getattr(mention, "key", "") or ""
        if key:
            mentions[key] = getattr(mention, "name", "") or ""
    return {
        "message_id": getattr(message, "message_id", "") or "",
        "thread_id": getattr(message, "thread_id", "") or "",
        "root_id": getattr(message, "root_id", "") or "",
        "create_time": _stamp(getattr(message, "create_time", 0)),
        "sender_type": getattr(message, "sender_type", None) or getattr(sender, "sender_type", "") or "",
        "sender_name": getattr(sender, "sender_name", "") or "",
        "sender_id": getattr(sender, "id", "") or "",
        "mentions": mentions,
        "message_type": getattr(message, "message_type", None) or getattr(message, "msg_type", None) or "text",
        "text": text_from_body(parsed),
        "image_key": (image_keys(parsed) or [""])[0],
        "image_keys": image_keys(parsed),
        "file_key": parsed.get("file_key") or "",
        "file_name": parsed.get("file_name") or "file",
    }


def _file_text(name: str, raw: bytes) -> str:
    if not raw or b"\x00" in raw[:1024]:
        return f"[file:{name}]"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"[file:{name}]"
    cleaned = clean(text)
    return (cleaned[:2000] if cleaned else f"[file:{name}]")


def join_pages(pages) -> list[dict]:
    items = []
    seen = set()
    for page in pages:
        for item in page:
            message_id = item.get("message_id")
            if message_id and message_id in seen:
                continue
            if message_id:
                seen.add(message_id)
            items.append(item)
    return items


def merged_items(current, pages) -> list[dict]:
    items = join_pages(pages)
    message_id = current.get("message_id")
    if not message_id or all(item.get("message_id") != message_id for item in items):
        items.append(current)
    return items


def _keys(item) -> list[str]:
    found = []
    for key in list(item.get("image_keys") or []) + [item.get("image_key") or ""]:
        if key and key not in found:
            found.append(key)
    return found


def _speaker(item, labels) -> str:
    name = (item.get("sender_name") or "").strip()
    if name:
        return name
    sid = item.get("sender_id") or ""
    if not sid:
        return "Someone"
    if sid not in labels:
        labels[sid] = f"Person {len(labels) + 1}"
    return labels[sid]


def bundle(items, fetch, *, supports_images: bool = True):
    lines = []
    images = []
    labels = {}
    for item in items:
        if item.get("sender_type") == "app":
            continue
        kind = item.get("message_type") or "text"
        keys = _keys(item)
        if supports_images:
            for key in keys:
                if len(images) >= 4:
                    break
                raw = fetch(item.get("message_id") or "", key, "image") or b""
                part = image_part(raw)
                if part:
                    images.append(part)
        if kind == "file" and item.get("file_key"):
            raw = fetch(item.get("message_id") or "", item["file_key"], "file") or b""
            line = _file_text(item.get("file_name") or "file", raw)
        elif kind in {"text", "post", "interactive"}:
            line = clean(show_mentions(item.get("text") or "", item.get("mentions") or {}))
        elif keys:
            line = "[image]"
        else:
            line = ""
        if line:
            lines.append(f"{_speaker(item, labels)}: {line}")
    return "\n".join(lines), images


def message_link_text(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    messages = data.get("messages") if isinstance(data, dict) else None
    if messages is None and isinstance(payload, dict):
        messages = payload.get("messages") or []
    parts = []
    for wrap in messages or []:
        if not isinstance(wrap, dict):
            continue
        message = wrap.get("message") if isinstance(wrap.get("message"), dict) else wrap
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        content = body.get("content") if isinstance(body, dict) else ""
        if isinstance(content, str):
            try:
                content = json.loads(content) if content else {}
            except json.JSONDecodeError:
                if content.strip():
                    parts.append(content.strip())
                continue
        if isinstance(content, dict):
            text = text_from_body(content)
            if text:
                parts.append(text)
    return "\n".join(parts)


def expand_message_links(text: str, resolve) -> str:
    def repl(match):
        url = match.group(0)
        try:
            got = message_link_text(resolve(url))
        except Exception as error:
            return f"{url}\n[message link unread: {clean(str(error))}]"
        if not got:
            return f"{url}\n[message link unread]"
        return f"{url}\n[linked message]\n{got}"

    return _MESSAGE_LINK.sub(repl, text or "")


def field_text(value) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(part for part in (field_text(item) for item in value) if part)
    if isinstance(value, dict):
        for key in ("text", "name", "link", "email"):
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
        return ""
    return ""


_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def record_attachments(fields: dict) -> list[dict]:
    found = []
    for name, value in (fields or {}).items():
        items = value if isinstance(value, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            token = item.get("file_token")
            if not isinstance(token, str) or not token:
                continue
            filename = str(item.get("name") or "")
            mime = str(item.get("type") or "")
            if not (mime.startswith("image/") or filename.lower().endswith(_IMAGE_EXT)):
                continue
            found.append({
                "field": name,
                "file_token": token,
                "filename": filename,
                "url": str(item.get("url") or ""),
            })
    return found


def attachment_extra(item: dict) -> str:
    url = str((item or {}).get("url") or "")
    got = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("extra") or []
    return got[0] if got else ""


def tmp_media_url(payload: dict, file_token: str) -> str:
    data = payload.get("data") if isinstance(payload, dict) else {}
    urls = data.get("tmp_download_urls") if isinstance(data, dict) else []
    for row in urls or []:
        if isinstance(row, dict) and row.get("file_token") == file_token:
            url = row.get("tmp_download_url")
            if isinstance(url, str):
                return url
    return ""


def _read_url(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.read()


def download_tmp_media(client, file_token: str, extra: str, opener=None, ask=None) -> bytes:
    # ponytail: advanced-permission Bases 403 the direct media GET. The temp URL works.
    from urllib.parse import quote

    uri = (
        "/open-apis/drive/v1/medias/batch_get_tmp_download_url"
        f"?file_tokens={quote(file_token, safe='')}&extra={quote(extra, safe='')}"
    )
    url = tmp_media_url((ask or _lark_json)(client, "GET", uri), file_token)
    if not url:
        return b""
    return (opener or _read_url)(url)


def record_images(fields: dict, fetch, *, limit: int = 4) -> list:
    parts = []
    for item in record_attachments(fields):
        if len(parts) >= limit:
            break
        try:
            raw = fetch(item) or b""
        except Exception as error:
            print(f"record image skipped token={item['file_token']} {clean(str(error))}", flush=True)
            continue
        part = image_part(raw)
        if part:
            parts.append(part)
    return parts


def bitable_extra(table_id: str, field_id: str, record_id: str, file_token: str) -> str:
    return json.dumps({
        "bitablePerm": {
            "tableId": table_id,
            "attachments": {field_id: {record_id: [file_token]}},
        }
    }, separators=(",", ":"))


def record_fields_text(fields: dict) -> str:
    lines = []
    images = {item["field"] for item in record_attachments(fields)}
    for name, value in fields.items():
        shown = field_text(value)
        if shown and name in images:
            shown = f"{shown} [image]"
        if shown:
            lines.append(f"{name}: {shown}")
    return "\n".join(lines)


def unwrap_record_urls(text: str) -> str:
    def repl(match):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(match.group(0)).query)
        target = query.get("url", [""])[0]
        return target if _RECORD_LINK.search(target) else match.group(0)

    return _WEB_URL.sub(repl, text or "")


def expand_record_links(text: str, resolve) -> str:
    text = unwrap_record_urls(text)

    def repl(match):
        url, token = match.group(0), match.group(1)
        try:
            got = record_fields_text(resolve(token))
        except Exception as error:
            return f"{url}\n[record link unread: {clean(str(error))}]"
        if not got:
            return f"{url}\n[record link unread]"
        return f"{url}\n[linked record]\n{clean(got)}"

    return _RECORD_LINK.sub(repl, text or "")


def _lark_json(client, method: str, uri: str, body=None) -> dict:
    from lark_oapi.core.enum import AccessTokenType, HttpMethod
    from lark_oapi.core.model import BaseRequest

    builder = (
        BaseRequest.builder()
        .http_method(HttpMethod[method])
        .uri(uri)
        .token_types({AccessTokenType.TENANT})
    )
    if body is not None:
        builder = builder.body(body)
    resp = client.request(builder.build())
    raw = b""
    if resp.raw is not None and resp.raw.content:
        raw = resp.raw.content
    try:
        payload = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = payload.get("code", resp.code)
    if code not in (0, None):
        raise RuntimeError(payload.get("msg") or f"lark failed code={code}")
    return payload


def query_record_share(client, token: str) -> dict:
    return _shared_record(client, token)[0]


def _shared_record(client, token: str) -> tuple:
    meta = _lark_json(client, "GET", f"/open-apis/base/v3/record_share/{token}/meta")
    data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
    base, table, record = data.get("base_token"), data.get("table_id"), data.get("record_id")
    if not base or not table or not record:
        raise RuntimeError("record link has no table")
    got = _lark_json(
        client,
        "POST",
        f"/open-apis/bitable/v1/apps/{base}/tables/{table}/records/batch_get",
        {"record_ids": [record]},
    )
    data = got.get("data") if isinstance(got.get("data"), dict) else {}
    records = data.get("records") or []
    fields = records[0].get("fields") if records and isinstance(records[0], dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    return base, table, record, fields


def _field_ids(client, base: str, table: str) -> dict:
    got = _lark_json(client, "GET", f"/open-apis/bitable/v1/apps/{base}/tables/{table}/fields?page_size=100")
    data = got.get("data") if isinstance(got.get("data"), dict) else {}
    return {
        item.get("field_name"): item.get("field_id")
        for item in data.get("items") or []
        if isinstance(item, dict) and item.get("field_name") and item.get("field_id")
    }


def download_bitable_file(client, file_token: str, extra: str) -> bytes:
    from urllib.parse import quote
    from lark_oapi.core.enum import AccessTokenType, HttpMethod
    from lark_oapi.core.model import BaseRequest

    uri = f"/open-apis/drive/v1/medias/{file_token}/download?extra={quote(extra, safe='')}"
    request = (
        BaseRequest.builder()
        .http_method(HttpMethod.GET)
        .uri(uri)
        .token_types({AccessTokenType.TENANT})
        .build()
    )
    resp = client.request(request)
    raw = b""
    if resp.raw is not None and resp.raw.content:
        raw = resp.raw.content
    if raw[:1] in (b"{", b"["):
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("msg") or f"lark failed code={payload.get('code')}")
    return raw


def load_shared_record(client, token: str) -> tuple:
    base, table, record, fields = _shared_record(client, token)
    if not supports_image_input() or not record_attachments(fields):
        return fields, []
    ids = _field_ids(client, base, table)

    def fetch(item):
        extra = attachment_extra(item)
        if extra:
            return download_tmp_media(client, item["file_token"], extra)
        field_id = ids.get(item["field"]) or ""
        if not field_id:
            return b""
        return download_bitable_file(
            client, item["file_token"], bitable_extra(table, field_id, record, item["file_token"]),
        )

    return fields, record_images(fields, fetch)


def query_message_link(client, url: str) -> dict:
    from lark_oapi.core.enum import AccessTokenType, HttpMethod
    from lark_oapi.core.model import BaseRequest

    request = (
        BaseRequest.builder()
        .http_method(HttpMethod.POST)
        .uri("/open-apis/im/v1/message_links/query")
        .token_types({AccessTokenType.TENANT})
        .body({"message_link_url": url})
        .build()
    )
    resp = client.request(request)
    raw = b""
    if resp.raw is not None and resp.raw.content:
        raw = resp.raw.content
    try:
        payload = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = payload.get("code", resp.code)
    if code not in (0, None):
        raise RuntimeError(payload.get("msg") or f"message link failed code={code}")
    return payload


def rc_target(text: str) -> str:
    for match in _VERSION_LINE.finditer(text or ""):
        value = match.group("value").strip()
        exact = _RC_BRANCH.search(value)
        if exact:
            return exact.group(0)
        number = _VERSION_NUMBER.search(value)
        if number:
            return number.group(0)
    return ""


def rc_branch_hint(text: str) -> str:
    target = rc_target(text)
    if not target:
        return ""
    if target.startswith("rc/"):
        return f"App version names branch {target}. Search that branch. Do not use develop."
    return (
        f"App version {target} is the release to search. "
        f"Call mobox_gitlab branches with {target!r} on each project you check. "
        "Use a matching rc/ branch. Do not use develop."
    )


def platform_scope(text: str) -> str:
    low = (text or "").lower()
    android = re.search(r"\bandroid\b|\bgohost\b", low) is not None
    ios = re.search(r"\bios\b|\biphone\b|\bconsumer-ios\b", low) is not None
    if android == ios:
        return ""
    return "gohost" if android else "ios"


def focus_line(scope: str) -> str:
    if scope == "ios":
        return "Search only ios/. Do not search gohost unless the developer asks for a cross-check."
    if scope == "gohost":
        return "Search only gohost/. Do not search ios unless the developer asks for a cross-check."
    return ""


def apply_local_checkouts(text: str, list_branches=None, use_checkout=None, *, develop_present: bool = False) -> str:
    line = focus_line(platform_scope(text))
    return f"{line}\n\n{text}" if line else text


def prompt_body(text: str, images: list, *, supports_images: bool) -> dict:
    guidance = ""
    hint = rc_branch_hint(text)
    if hint:
        guidance = hint + "\n\n"
    if "[linked record]" in text:
        guidance = _LINKED_RECORD_GUIDANCE + "\n\n" + guidance
        text = _LEADING_GREETING.sub(r"\1", text, count=1)
    if not supports_images:
        return {"text": f"{guidance}{_TEXT_ONLY_EVIDENCE}\n\n{text}"}
    body = {"text": guidance + text}
    files = [{"uri": item["url"], "name": item.get("filename") or "qa.jpg"} for item in images if item.get("url")]
    if files:
        body["files"] = files
    return body


def prompt_from_pages(current, pages, fetch) -> str:
    return bundle(merged_items(current, pages), fetch)[0]


class ThreadLog:
    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    def remember(self, message) -> None:
        item = item_from(message)
        message_id = item.get("message_id")
        if not message_id or item.get("sender_type") == "app":
            return
        item["thread_id"] = getattr(message, "thread_id", "") or item.get("thread_id") or ""
        item["root_id"] = getattr(message, "root_id", "") or ""
        item["create_time"] = _stamp(getattr(message, "create_time", 0))
        with self._lock:
            self._items[message_id] = item

    def related(self, thread_id: str, root_id: str) -> list[dict]:
        with self._lock:
            found = [
                dict(item)
                for item in self._items.values()
                if (thread_id and item.get("thread_id") == thread_id)
                or (root_id and (item.get("root_id") == root_id or item.get("message_id") == root_id))
            ]
        return _by_time(found)


def _stamp(raw) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _by_time(items: list[dict]) -> list[dict]:
    if not any(item.get("create_time") for item in items):
        return items
    return sorted(items, key=lambda item: item.get("create_time") or 0)


thread_log = ThreadLog()


class SentLines:
    def __init__(self):
        self._ids = {}
        self._lock = threading.Lock()

    def fresh(self, root_id: str, items: list) -> list:
        # ponytail: in memory, marked when built. A bridge restart resends the thread once. A failed turn does not.
        with self._lock:
            sent = self._ids.setdefault(root_id, set())
            chosen = [item for item in items if not item.get("message_id") or item["message_id"] not in sent]
            sent.update(item["message_id"] for item in chosen if item.get("message_id"))
            return chosen


sent_lines = SentLines()


def ancestor_ids(message, items) -> list[str]:
    have = {item.get("message_id") for item in items}
    found = []
    for key in ("root_id", "parent_id"):
        message_id = getattr(message, key, "") or ""
        if message_id and message_id not in have and message_id not in found:
            found.append(message_id)
    return found


def thread_prompt(items, fetch) -> str:
    return bundle(items, fetch)[0]


def _bot_mentioned(message) -> bool:
    bot_open_id = os.environ.get("LARK_BOT_OPEN_ID", "")
    mentions = getattr(message, "mentions", None) or []
    open_ids = [getattr(getattr(item, "id", None), "open_id", "") or "" for item in mentions]
    if bot_open_id:
        return bot_open_id in open_ids
    return bool(open_ids)


def _group_watched(message) -> bool:
    return getattr(message, "chat_type", "") == "group" and getattr(message, "chat_id", "") in watch_channels()


def allowed(data) -> bool:
    event = data.event
    sender = getattr(event, "sender", None)
    if getattr(sender, "sender_type", "") == "app":
        return False
    message = event.message
    if getattr(message, "chat_type", "") == "p2p":
        return True
    if not _group_watched(message):
        return False
    return _bot_mentioned(message) or _tag_mentioned(message)


def _tag_mentioned(message) -> bool:
    tags = {part.strip() for part in os.environ.get("LARK_TAG_OPEN_IDS", "").split(",") if part.strip()}
    if not tags:
        return False
    mentions = getattr(message, "mentions", None) or []
    open_ids = [getattr(getattr(item, "id", None), "open_id", "") or "" for item in mentions]
    return any(item in tags for item in open_ids)


_STATUS = {
    "think": "Thinking....",
    "thread": "[1/3] Reading the thread",
    "search": "[2/3] Searching the repos",
    "write": "[3/3] Writing the triage",
    "ready": "Triage ready",
    "fail": "Stopped",
}


def status_line(step: str, detail: str = "") -> str:
    line = _STATUS[step]
    detail = (detail or "").strip()
    if not detail:
        return line
    return f"{line} · {detail[:80]}"


def investigation_updates(phase: str, event: str, detail: str = "") -> tuple[str, list[str]]:
    if event == "tool" and phase == "thinking":
        return "search", [status_line("thread"), status_line("search", detail)]
    if event == "tool":
        return "search", [status_line("search", detail)]
    if event == "write" and phase != "thinking":
        return "write", [status_line("write")]
    return phase, []


def clue_from(text: str) -> str:
    low = text.lower()
    android = "android" in low
    ios = re.search(r"\bios\b", low) is not None
    if android and ios:
        repo = "both repos"
    elif android:
        repo = "gohost"
    elif ios:
        repo = "ios"
    else:
        repo = ""
    branch = ""
    match = re.search(r"\brc/[A-Za-z0-9._-]+", text)
    if match:
        branch = match.group(0)
    elif re.search(r"\bdevelop\b", text):
        branch = "develop"
    return " · ".join(part for part in (repo, branch) if part)


_CODE_CHECK = (
    "You have not read the repository. Call mobox_gitlab with operation search on the platform and branch, "
    "then call it with operation file on the GitLab path that decides the behavior. Rewrite the summary. "
    "Hypothesis must name that file and the condition. If the search has no hit, confidence is low "
    "and name the command you ran."
)


def _tool_blob(part: dict) -> str:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    chunks = [part.get("tool"), part.get("name"), state.get("title")]
    entered = state.get("input") if isinstance(state.get("input"), dict) else {}
    chunks.append(entered.get("command") or entered.get("cmd"))
    return " ".join(str(item) for item in chunks if item).lower()


def repo_was_read(messages) -> bool:
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        parts = item.get("parts") or item.get("content") or []
        for part in parts:
            if not isinstance(part, dict) or part.get("type") not in {"tool", "tool-call", "tool_use"}:
                continue
            state = part.get("state") or {}
            if state.get("status") not in {None, "completed"}:
                continue
            if (part.get("tool") or part.get("name")) == "mobox_gitlab":
                if (state.get("input") or {}).get("operation") in {"search", "file"}:
                    return True
            blob = _tool_blob(part)
            if "mobox-glab" in blob and ("search" in blob or "file" in blob):
                return True
    return False


def needs_summary_shape(answer: str) -> bool:
    text = answer or ""
    if "**Hypothesis:**" in text:
        return False
    low = text.lower()
    marks = ("i'll start", "i will start", "let me begin", "let me try", "i'll search", "searching for the")
    if any(mark in low for mark in marks):
        return True
    return "hypothesis" in low and ("confidence" in low or "action item" in low)


_SUMMARY_SHAPE = (
    "Stop. The developer sees one card. Reply with only these fields, one short sentence each. No narration.\n"
    "**Hypothesis:** the file and the condition, or that the search had no hit\n"
    "**FE or BE:** FE or BE and why\n"
    "**Checked:** the branch you searched\n"
    "**Confidence:** high, medium, or low\n"
    "**Question:** none, or one question"
)


def needs_code_check(answer: str, messages) -> bool:
    if "**Hypothesis:**" not in (answer or "") and "**Issue:**" not in (answer or ""):
        return False
    return not repo_was_read(messages)


def tool_beat(messages) -> str:
    title = ""
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        parts = item.get("parts") or item.get("content") or []
        for part in parts:
            if not isinstance(part, dict) or part.get("type") not in {"tool", "tool-call", "tool_use"}:
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            title = state.get("title") or part.get("tool") or part.get("name") or title
    return str(title)[:80]


def dispatch(dedupe_id, message_id, text, reply_fn, agent, tracker, on_offer=None, on_status=None) -> None:
    if tracker.skip(dedupe_id):
        return
    tell = on_status or (lambda line: reply_fn(message_id, line))
    try:
        cleaned = clean(text)
        if not cleaned:
            reply_fn(message_id, "Tell me what you need: a bug, a question about the code, a small change, or an MR to review.")
        else:
            try:
                answer = clean(agent(cleaned))
            except Exception as error:
                answer = f"Agent failed: {clean(str(error))}"
                print(f"agent failed message_id={message_id}", flush=True)
                tell(status_line("fail"))
                reply_fn(message_id, answer)
                tracker.complete(dedupe_id)
                return
            if on_offer is not None and mr_flow.is_triage(answer):
                tell(status_line("ready"))
                on_offer(message_id, answer)
            else:
                reply_fn(message_id, answer)
    except Exception:
        tracker.release(dedupe_id)
        raise
    tracker.complete(dedupe_id)


def load_env(path: Path) -> None:
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def _prompt_images(body: dict) -> list:
    images = []
    for item in body.get("files") or []:
        uri = item.get("uri") or ""
        if not uri.startswith("data:") or ";base64," not in uri:
            continue
        header, data = uri.split(";base64,", 1)
        images.append({"mime": header[5:] or "image/jpeg", "data": data})
    return images


def ask(session_id: str, body: dict, timeout=900) -> dict:
    payload = json.dumps({
        "session_id": session_id or "",
        "text": body.get("text") or "",
        "images": _prompt_images(body),
    }).encode()
    request = urllib.request.Request(
        CURSOR_AGENT_URL + "/v1/prompt",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            parsed = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"cursor-agent POST /v1/prompt -> {error.code} {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"cursor-agent is not running at {CURSOR_AGENT_URL}: {error.reason}") from error
    if not isinstance(parsed, dict) or not parsed.get("session_id"):
        raise RuntimeError("cursor-agent did not return a session id")
    return parsed


class Sessions:
    def __init__(self, path=""):
        self._path = path
        self._ids = {}
        self._lock = threading.Lock()
        if path and os.path.exists(path):
            try:
                self._ids = json.loads(Path(path).read_text()) or {}
            except (OSError, json.JSONDecodeError):
                self._ids = {}

    def _save(self) -> None:
        # ponytail: whole map rewritten each change. Fine for hackathon thread counts.
        if self._path:
            Path(self._path).write_text(json.dumps(self._ids))

    def lookup(self, key: str) -> str:
        with self._lock:
            return self._ids.get(key, "")

    def open(self, key, create):
        keys = [key] if isinstance(key, str) else list(key or [])
        keys = [item for item in keys if item]
        if not keys:
            return create()
        with self._lock:
            found = next((self._ids[item] for item in keys if item in self._ids), "")
        if not found:
            found = create()
        with self._lock:
            found = next((self._ids[item] for item in keys if item in self._ids), found)
            for item in keys:
                self._ids.setdefault(item, found)
            self._save()
            return found

    def drop(self, key: str) -> None:
        with self._lock:
            found = self._ids.get(key)
            if not found:
                self._ids.pop(key, None)
                return
            for item in [item for item, value in self._ids.items() if value == found]:
                self._ids.pop(item, None)
            self._save()

    def replace(self, old_session: str, new_session: str) -> None:
        with self._lock:
            for key, session in self._ids.items():
                if session == old_session:
                    self._ids[key] = new_session
            self._save()


def _ids(message) -> list[str]:
    if message is None:
        return []
    if isinstance(message, dict):
        getter = message.get
    else:
        getter = lambda name: getattr(message, name, "") or ""
    found = []
    for name in ("root_id", "thread_id", "message_id", "parent_id"):
        value = getter(name) or ""
        if value and value not in found:
            found.append(value)
    return found


def session_keys(*messages) -> list[str]:
    found = []
    for message in messages:
        for value in _ids(message):
            if value not in found:
                found.append(value)
    return found


def thread_session_keys(message, anchor=None):
    thread_id = getattr(message, "thread_id", "") or ""
    root_id = getattr(message, "root_id", "") or getattr(message, "message_id", "") or ""
    if isinstance(anchor, dict):
        thread_id = thread_id or anchor.get("thread_id") or ""
    elif anchor is not None:
        thread_id = thread_id or getattr(anchor, "thread_id", "") or ""
    return session_keys(message, anchor, *thread_log.related(thread_id, root_id))


def session_key(message) -> str:
    keys = session_keys(message)
    return keys[0] if keys else ""


def reply_ready(previous: str, current: str, outcome) -> bool:
    if not current or not outcome:
        return False
    if previous and current == previous:
        return False
    return True


def session_missing(error: BaseException) -> bool:
    return "-> 404" in str(error)


def _parts_text(item: dict) -> list[str]:
    chunks = []
    for part in item.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            chunks.append(part["text"])
    return chunks


def assistant_text(messages: list) -> str:
    chunks = []
    for item in messages:
        if not isinstance(item, dict) or item.get("type") != "assistant":
            continue
        chunks.extend(_parts_text(item))
    return "\n".join(chunks).strip()


def _ordered_messages(messages: list) -> list:
    def stamp(item: dict):
        info = item.get("time")
        if not isinstance(info, dict):
            return None
        for name in ("created", "start"):
            value = info.get(name)
            if isinstance(value, (int, float)):
                return value
        return None

    items = [item for item in messages or [] if isinstance(item, dict)]
    if any(stamp(item) is not None for item in items):
        items.sort(key=lambda item: stamp(item) or 0)
    return items


def _messages_after(messages: list, user_id: str) -> list:
    items = _ordered_messages(messages)
    if not user_id:
        return items
    for index, item in enumerate(items):
        if item.get("id") == user_id:
            return items[index + 1:]
    return []


def reply_after(messages: list, user_id: str) -> str:
    return assistant_text(_messages_after(messages, user_id))


def _retry_file_evidence(messages: list) -> list[str]:
    excerpts = []
    for item in messages:
        for part in item.get("content") or []:
            if part.get("type") != "tool" or (part.get("name") or part.get("tool")) != "mobox_gitlab":
                continue
            state = part.get("state") or {}
            inputs = state.get("input") or {}
            args = inputs.get("args") or []
            if state.get("status") != "completed" or inputs.get("operation") != "file" or len(args) != 2:
                continue
            text = "\n".join(_parts_text(state)).strip()
            if text:
                branch, path = args
                excerpts.append(f"{inputs.get('project', '')} {branch} {path}\n{text[:1100]}")
    return excerpts[-4:]


def _retry_context(history: list, current: str, file_evidence: list[str]) -> tuple[str, bool]:
    ordered = _ordered_messages(history)
    previous_requests = []
    for item in ordered:
        if item.get("type") != "user":
            continue
        text = (item.get("text") or "").removeprefix(_LINKED_RECORD_GUIDANCE + "\n\n")
        text = text.removeprefix(_TEXT_ONLY_EVIDENCE + "\n\n").strip()
        if text and text not in {_CODE_CHECK, _SUMMARY_SHAPE, current.strip()}:
            previous_requests.append(text)
    if not previous_requests and not file_evidence:
        return "", False

    parts = []
    if previous_requests:
        context = "\n\n".join(previous_requests)
        if len(context) > 5000:
            context = context[:2000] + "\n\n[Some earlier context omitted]\n\n" + context[-3000:]
        parts.append(f"Earlier Lark messages:\n{context}")
    prior_findings = ""
    if repo_was_read(ordered):
        prior_findings = next(("\n".join(_parts_text(item)) for item in reversed(ordered)
                               if item.get("type") == "assistant" and _parts_text(item)), "")
        if prior_findings:
            parts.append(f"Previous investigation findings:\n{prior_findings[:2500]}")
    if file_evidence:
        parts.append("GitLab file excerpts from this investigation:\n" + "\n\n".join(file_evidence))
        parts.append(
            "Answer using these completed file reads. Cite the path and branch of any evidence you use. "
            "If the excerpts do not establish a cause, state what is still unclear and keep confidence low."
        )
    parts.append(f"Latest Lark message. Use this evidence to continue the same investigation:\n{current}")
    return "\n\n".join(parts), bool(prior_findings or file_evidence)


sessions = Sessions(str(Path(__file__).with_name(".sessions.json")))


def _turn_error(turn: dict, user_id: str) -> dict:
    error = turn.get("error")
    if isinstance(error, dict):
        return error
    messages = turn.get("messages") or []
    return next((item["error"] for item in reversed(_messages_after(messages, user_id))
                 if isinstance(item.get("error"), dict)), {})


def _remember(keys: list[str], previous: str, session_id: str) -> None:
    if previous and previous != session_id:
        sessions.replace(previous, session_id)
    sessions.open(keys, lambda: session_id)


def run_agent(text: str, images=None, session_key="", on_status=None) -> str:
    text = apply_local_checkouts(text)
    keys = [session_key] if isinstance(session_key, str) else list(session_key or [])
    keys = [item for item in keys if item]
    existing = next((sessions.lookup(item) for item in keys if sessions.lookup(item)), "")
    supports_images = supports_image_input()
    body = prompt_body(text, images or [], supports_images=supports_images)

    def submit(session_id: str, payload: dict) -> dict:
        try:
            return ask(session_id, payload, timeout=900)
        except Exception as error:
            if session_id and keys and session_missing(error):
                sessions.drop(keys[0])
                return ask("", payload, timeout=900)
            raise

    turn = submit(existing, body)
    session_id = turn["session_id"]
    _remember(keys, existing, session_id)
    user_id = turn.get("user_id") or ""
    turn_user_id = user_id
    last = ""
    phase = "thinking"
    checked = False
    shaped = False
    retried = False
    reused_findings = False

    def note(event: str, detail: str = "") -> None:
        nonlocal last, phase
        if on_status is None:
            return
        phase, lines = investigation_updates(phase, event, detail)
        for line in lines:
            if line == last:
                continue
            last = line
            on_status(line)

    for _ in range(6):
        outcome = turn.get("outcome") or ""
        reply = turn.get("reply") or ""
        messages = turn.get("messages") or []
        if outcome == "failed":
            error = _turn_error(turn, user_id)
            overflow = error.get("type") == "provider.invalid-request" and error.get("status") == 422
            if not retried and overflow:
                file_evidence = _retry_file_evidence(_messages_after(messages, turn_user_id))
                retry_text, reused_findings = _retry_context(messages, text, file_evidence)
                if retry_text:
                    retry_body = prompt_body(retry_text, images or [], supports_images=supports_images)
                    turn = submit("", retry_body)
                    new_session = turn["session_id"]
                    _remember(keys, session_id, new_session)
                    session_id = new_session
                    user_id = turn.get("user_id") or ""
                    checked = shaped = False
                    retried = True
                    continue
            return "The model could not process this request. The investigation could not be completed."
        beat = tool_beat(messages)
        if beat:
            note("tool", beat)
        elif reply:
            note("write")
        if not reply:
            return f"Cursor agent finished with outcome {outcome or 'finished'} and no text."
        nudge = ""
        if not shaped and needs_summary_shape(reply):
            shaped = True
            nudge = _SUMMARY_SHAPE
        elif not checked and not reused_findings and needs_code_check(reply, messages):
            checked = True
            note("tool", "mobox-glab search")
            nudge = _CODE_CHECK
        if nudge:
            turn = submit(session_id, {"text": nudge})
            session_id = turn.get("session_id") or session_id
            user_id = turn.get("user_id") or user_id
            continue
        return reply[:12000]
    return "Cursor agent did not finish within 15 minutes."


def reply(client: lark.Client, message_id: str, text: str, *, in_thread: bool = False) -> str:
    sent, _thread_id = _send(client, message_id, "text", json.dumps({"text": text}), in_thread=in_thread)
    return sent


def _update(client: lark.Client, message_id: str, msg_type: str, content: str) -> bool:
    from lark_oapi.api.im.v1.model.update_message_request import UpdateMessageRequest
    from lark_oapi.api.im.v1.model.update_message_request_body import UpdateMessageRequestBody

    body = UpdateMessageRequestBody.builder().msg_type(msg_type).content(content).build()
    request = UpdateMessageRequest.builder().message_id(message_id).request_body(body).build()
    response = client.im.v1.message.update(request)
    if not response.success():
        print(f"status update failed code={response.code}", flush=True)
        return False
    return True


def update_text(client: lark.Client, message_id: str, text: str) -> bool:
    return _update(client, message_id, "text", json.dumps({"text": text}))


def update_card(client: lark.Client, message_id: str, card: dict) -> bool:
    from lark_oapi.api.im.v1.model.patch_message_request import PatchMessageRequest
    from lark_oapi.api.im.v1.model.patch_message_request_body import PatchMessageRequestBody

    body = PatchMessageRequestBody.builder().content(json.dumps(card)).build()
    request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
    response = client.im.v1.message.patch(request)
    if not response.success():
        print(f"status update failed code={response.code}", flush=True)
        return False
    return True


def reply_card(client: lark.Client, message_id: str, card: dict, *, in_thread: bool = False) -> None:
    _send(client, message_id, "interactive", json.dumps(card), in_thread=in_thread)


def _send(client: lark.Client, message_id: str, msg_type: str, content: str, *, in_thread: bool = False):
    builder = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
    if in_thread:
        builder.reply_in_thread(True)
    body = builder.build()
    request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    response = client.im.v1.message.reply(request)
    if not response.success():
        raise RuntimeError(f"lark reply failed code={response.code}")
    data = response.data
    return getattr(data, "message_id", "") or "", getattr(data, "thread_id", "") or ""


def sender_open_id(data) -> str:
    sender = getattr(data.event, "sender", None)
    ident = getattr(sender, "sender_id", None)
    return getattr(ident, "open_id", "") or ""


def run_draft(item: dict) -> str:
    return "\n".join(_draft_one(item, project) for project in item["projects"])


def _draft_one(item: dict, project: str) -> str:
    prompt = (
        f"Continue this session. Write one unified diff for {project} only, for the confirmed change. "
        "Paths are relative to that repository root. Use the files already read in this session. "
        "Output only a ```diff fenced block. Do not push. Do not open an MR.\n\n"
        + item["summary"]
    )
    diff = mr_flow.extract_diff(clean(run_agent(prompt, session_key=item.get("session_key", ""))))
    if not diff:
        return f"{project}: no patch. Draft MR not opened."
    work = Path(os.environ.get("MOBOX_WORK", "/tmp")) / "mobox-diffs"
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"{item['issue']}-{project}.diff"
    path.write_text(diff + "\n")
    desc = work / f"{item['issue']}.txt"
    desc.write_text(mr_flow.draft_description(item.get("summary", ""), item.get("requester", "")))
    script = Path(__file__).resolve().parent / "bin" / "mobox-draft"
    command = [str(script), "--confirm", project, item["branch"], item["issue"], str(path), str(desc)]
    assignee = mr_flow.gitlab_assignee(item.get("requester", ""))
    if assignee:
        command.append(assignee)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        err = clean((result.stderr or result.stdout or "draft failed")[-400:])
        return f"{project}: Draft MR failed: {err}"
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return f"{project}: {lines[-1]}" if lines else f"{project}: Draft MR failed: no url"


pending = mr_flow.Pending()


def _toast(text: str, card: dict | None = None) -> P2CardActionTriggerResponse:
    toast = CallBackToast()
    toast.type = "info"
    toast.content = text[:200]
    response = P2CardActionTriggerResponse()
    response.toast = toast
    if card is not None:
        callback = CallBackCard()
        callback.type = "raw"
        callback.data = card
        response.card = callback
    return response


def _say(client, message_id: str, text: str) -> None:
    if message_id:
        reply(client, message_id, text)


def on_card(client: lark.Client, data):
    action_id, choice, clicker, message_id = mr_flow.card_fields(data)
    now = time.time()
    if choice == "not_useful":
        pending.drop(action_id)
        _say(client, message_id, "noted")
        return _toast("noted")
    if choice == "cross_check":
        item = pending.peek(action_id, now)
        if item is None:
            _say(client, message_id, "expired or not your request")
            return _toast("expired or not your request")
        source = item.get("source") or item.get("summary") or ""

        def cross():
            try:
                answer = clean(run_agent(
                    "Cross-check the other platform for this report.\n\n" + source,
                    session_key=item.get("session_key", ""),
                ))
                reply_card(client, message_id, mr_flow.triage_card(answer, action_id))
            except Exception as error:
                print(f"cross-check failed {clean(str(error))}", flush=True)

        threading.Thread(target=cross, daemon=True).start()
        return _toast("cross-check")
    if choice == "cancel":
        text = mr_flow.confirm_action(pending, action_id, "cancel", clicker, now, lambda _item: "")
        _say(client, message_id, text)
        return _toast(text)
    if choice not in {"confirm", "raise_mr"}:
        _say(client, message_id, "noted")
        return _toast("noted")
    peeked = pending.peek(action_id, now)
    if peeked and peeked.get("draft_error"):
        taken = pending.take(action_id, clicker, now)
        text = peeked["draft_error"] if taken else "expired or not your request"
        _say(client, message_id, text)
        return _toast(text)
    item = pending.take(action_id, clicker, now)
    if item is None:
        _say(client, message_id, "expired or not your request")
        return _toast("expired or not your request")

    def work():
        wait_id = ""
        try:
            wait_id, _thread = _send(
                client, message_id, "interactive", json.dumps(mr_flow.loading_card()), in_thread=True,
            )
        except Exception as error:
            print(f"draft status failed {clean(str(error))}", flush=True)
        try:
            result = run_draft(item)
        except Exception as error:
            print(f"draft failed {clean(str(error))}", flush=True)
            result = f"Draft MR failed: {clean(str(error))}"
        done = {line.split(":", 1)[0] for line in result.splitlines() if "http" in line}
        left = [project for project in item.get("projects", []) if project not in done]
        if left:
            pending.put(action_id, clicker, {**item, "projects": left}, time.time())
            result += "\nTap Raise MR on the card again to retry " + ", ".join(left) + "."
        shown = mr_flow.result_card(result)
        if wait_id:
            try:
                update_card(client, wait_id, shown)
            except Exception as error:
                print(f"card update failed {clean(str(error))}", flush=True)
                reply(client, message_id, result)
        else:
            reply(client, message_id, result)

    threading.Thread(target=work, daemon=True).start()
    return _toast("opening draft MR")


def list_thread_items(client, thread_id: str) -> list[dict]:
    from lark_oapi.api.im.v1.model.list_message_request import ListMessageRequest

    # ponytail: 5 pages. A longer FCT thread is cut. Upgrade path is to follow has_more with no cap.
    pages = []
    token = ""
    for _ in range(5):
        builder = (
            ListMessageRequest.builder()
            .container_id_type("thread")
            .container_id(thread_id)
            .sort_type("ByCreateTimeAsc")
            .page_size(50)
        )
        if token:
            builder = builder.page_token(token)
        response = client.im.v1.message.list(builder.build())
        if not response.success():
            raise RuntimeError(f"list message failed code={response.code}")
        data = response.data
        found = data.items if data and data.items else []
        pages.append([item_from(item) for item in found])
        if not data or not data.has_more or not data.page_token:
            break
        token = data.page_token
    return join_pages(pages)


def get_message_item(client, message_id: str):
    from lark_oapi.api.im.v1.model.get_message_request import GetMessageRequest

    request = GetMessageRequest.builder().message_id(message_id).build()
    response = client.im.v1.message.get(request)
    if not response.success() or not response.data or not response.data.items:
        raise RuntimeError(f"get message failed code={response.code}")
    return response.data.items[0]


def download_resource(client, message_id: str, file_key: str, kind: str) -> bytes:
    from lark_oapi.api.im.v1.model.get_message_resource_request import GetMessageResourceRequest

    request = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type(kind)
        .build()
    )
    response = client.im.v1.message_resource.get(request)
    if not response.success() or response.file is None:
        raise RuntimeError(f"resource failed code={response.code}")
    return response.file.read()


def thread_id_of(client, message) -> str:
    found = getattr(message, "thread_id", "") or ""
    if found:
        return found
    root_id = getattr(message, "root_id", "") or ""
    if not root_id:
        return ""
    try:
        root = item_from(get_message_item(client, root_id))
    except Exception as error:
        print(f"thread id skipped message_id={root_id} {clean(str(error))}", flush=True)
        return ""
    return root.get("thread_id") or ""


class ThreadCursor:
    def __init__(self):
        self._last = {}
        self._lock = threading.Lock()

    def slice(self, key: str, items: list) -> list:
        if not key:
            return items
        with self._lock:
            last = self._last.get(key, "")
            ids = [item.get("message_id") for item in items]
            chosen = items[ids.index(last) + 1:] if last and last in ids else list(items)
            for item in reversed(items):
                if item.get("message_id"):
                    self._last[key] = item["message_id"]
                    break
            return chosen


thread_cursor = ThreadCursor()


def build_context(client, message):
    current = item_from(message)
    thread_id = current.get("thread_id") or thread_id_of(client, message)
    items = []
    if thread_id:
        try:
            items = list_thread_items(client, thread_id)
        except Exception as error:
            print(f"thread list skipped thread_id={thread_id} {clean(str(error))}", flush=True)
    root_id = getattr(message, "root_id", "") or current.get("root_id") or ""
    extras = []
    for message_id in ancestor_ids(message, items + [current]):
        try:
            extras.append(item_from(get_message_item(client, message_id)))
        except Exception as error:
            print(f"thread parent skipped message_id={message_id} {clean(str(error))}", flush=True)
    logged = thread_log.related(thread_id, root_id)
    pages = [page for page in (extras, items, logged) if page]
    chosen = sent_lines.fresh(root_id or current.get("message_id") or "", _by_time(merged_items(current, pages)))

    def fetch(mid, key, kind):
        try:
            return download_resource(client, mid, key, kind)
        except Exception as error:
            print(f"resource skipped message_id={mid} {clean(str(error))}", flush=True)
            return b""

    text, images = bundle(chosen, fetch, supports_images=supports_image_input())
    text = expand_message_links(text, lambda url: query_message_link(client, url))
    record_images_found = []

    def resolve(token):
        fields, parts = load_shared_record(client, token)
        record_images_found.extend(parts)
        return fields

    text = expand_record_links(text, resolve)
    return text, (images + record_images_found)[:4]


def handle(client: lark.Client, data) -> None:
    message = data.event.message
    sender = getattr(data.event, "sender", None)
    if getattr(sender, "sender_type", "") != "app" and (
        getattr(message, "chat_type", "") == "p2p" or _group_watched(message)
    ):
        thread_log.remember(message)
    if not allowed(data):
        return
    message_id = message.message_id
    dedupe_id = event_key(data)
    in_thread = getattr(message, "chat_type", "") != "p2p"

    def work():
        posted = {"id": "", "line": "", "thread_id": ""}

        def show(line: str) -> None:
            if line == posted["line"]:
                return
            posted["line"] = line
            card = mr_flow.status_card(line)
            try:
                if posted["id"]:
                    update_card(client, posted["id"], card)
                else:
                    posted["id"], posted["thread_id"] = _send(
                        client, message_id, "interactive", json.dumps(card), in_thread=in_thread
                    )
            except Exception as error:
                print(f"status failed {clean(str(error))}", flush=True)

        try:
            show(status_line("think"))
            text, images = build_context(client, message)
            sent_id = posted.get("id") or ""
            anchor = {"message_id": sent_id, "thread_id": posted.get("thread_id") or ""}
            if sent_id and not anchor["thread_id"] and not (getattr(message, "thread_id", "") or ""):
                try:
                    anchor = get_message_item(client, sent_id)
                except Exception as error:
                    print(f"thread id skipped message_id={sent_id} {clean(str(error))}", flush=True)
            key = thread_session_keys(message, anchor)
            print(f"mention message_id={message_id} chars={len(clean(text))} images={len(images)}", flush=True)
            def show_card(answer: str, action_id: str = "") -> None:
                card = mr_flow.answer_card(answer, action_id)
                posted["line"] = answer
                if posted["id"]:
                    update_card(client, posted["id"], card)
                    return
                reply_card(client, message_id, card, in_thread=in_thread)

            def on_offer(mid, answer):
                requester = sender_open_id(data)
                item, err = mr_flow.prepare_draft(answer, text, message_id)
                payload = dict(item or {})
                payload["summary"] = answer
                payload["source"] = text
                payload["requester"] = requester
                payload["session_key"] = key[0] if key else ""
                payload["draft_error"] = err or ""
                action_id = uuid.uuid4().hex
                if requester:
                    pending.put(action_id, requester, payload, time.time())
                show_card(answer, action_id)

            dispatch(
                dedupe_id,
                message_id,
                text,
                lambda _mid, body: show_card(body),
                lambda body: run_agent(body, images, key, show),
                seen,
                on_offer,
                show,
            )
        except Exception as error:
            print(f"bridge failed message_id={message_id} {clean(str(error))}", flush=True)
            show(status_line("fail"))

    threading.Thread(target=work, daemon=True).start()


def main() -> None:
    env_path = Path(sys.argv[1] if len(sys.argv) > 1 else "container_test/.env")
    load_env(env_path)
    app_id = os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not app_secret:
        raise SystemExit(f"LARK_APP_ID and LARK_APP_SECRET must be set in {env_path}")
    client = (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .domain(lark.LARK_DOMAIN)
        .timeout(20)
        .build()
    )
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(lambda data: handle(client, data))
        .register_p2_card_action_trigger(lambda data: on_card(client, data))
        .build()
    )
    ws = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.DEBUG,
        domain=lark.LARK_DOMAIN,
    )
    print("lark bridge listening", flush=True)
    ws.start()


if __name__ == "__main__":
    main()
