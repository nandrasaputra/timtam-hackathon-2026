#!/usr/bin/env python3
"""Lark @mention bridge. Runs on the host. Forwards one message to OpenCode, then replies in the thread."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
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

OPENCODE_URL = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096")
POLL_SECONDS = 3
POLL_LIMIT = 300
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


def image_part(raw: bytes):
    # ponytail: 1.5MB each, 4 images. A bigger QA dump is skipped. Upgrade path is a vision resize.
    mime = sniff_mime(raw)
    if not mime or len(raw) > 1_500_000:
        return None
    ext = "jpg" if mime == "image/jpeg" else mime.split("/")[1]
    encoded = base64.b64encode(raw).decode()
    return {"type": "file", "mime": mime, "filename": f"qa.{ext}", "url": f"data:{mime};base64,{encoded}"}


def text_from_body(parsed: dict) -> str:
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


def bundle(items, fetch):
    lines = []
    images = []
    labels = {}
    for item in items:
        if item.get("sender_type") == "app":
            continue
        kind = item.get("message_type") or "text"
        for key in _keys(item):
            if len(images) >= 4:
                break
            raw = fetch(item.get("message_id") or "", key, "image") or b""
            part = image_part(raw)
            if part:
                images.append(part)
        if kind == "file" and item.get("file_key"):
            raw = fetch(item.get("message_id") or "", item["file_key"], "file") or b""
            line = _file_text(item.get("file_name") or "file", raw)
        elif kind in {"text", "post"}:
            line = clean(show_mentions(item.get("text") or "", item.get("mentions") or {}))
        elif _keys(item):
            line = "[image]"
        else:
            line = ""
        if line:
            lines.append(f"{_speaker(item, labels)}: {line}")
    return "\n".join(lines), images


def prompt_body(text: str, images: list) -> dict:
    body = {"text": text}
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


def opencode_token() -> str:
    password = os.environ.get("OPENCODE_SERVER_PASSWORD") or os.environ.get("OPENCODE_PASSWORD") or ""
    if not password:
        raise SystemExit("OPENCODE_SERVER_PASSWORD is empty")
    return base64.b64encode(f"opencode:{password}".encode()).decode()


def opencode(method: str, path: str, payload=None, timeout=60):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        OPENCODE_URL + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Basic " + opencode_token(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"opencode {method} {path} -> {error.code} {body}") from error


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


def reply_after(messages: list, user_id: str) -> str:
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
    chunks = []
    seen = False
    for item in items:
        if item.get("id") == user_id:
            seen = True
            chunks = []
            continue
        if seen and item.get("type") == "assistant":
            chunks.extend(_parts_text(item))
    return "\n".join(chunks).strip() if seen else ""


sessions = Sessions(str(Path(__file__).with_name(".sessions.json")))


def _create_session() -> str:
    created = opencode("POST", "/api/session", {})
    session_id = (created.get("data") or created).get("id")
    if not session_id:
        raise RuntimeError("opencode did not return a session id")
    return session_id


def _message_page(listed) -> list:
    if isinstance(listed, list):
        return listed
    if not isinstance(listed, dict):
        return []
    data = listed.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        nested = data.get("items") or data.get("messages") or []
        return nested if isinstance(nested, list) else []
    return []


def _session_reply(session_id: str):
    info = opencode("GET", f"/api/session/{session_id}")
    session = info.get("data") if isinstance(info, dict) else None
    if not isinstance(session, dict):
        session = info if isinstance(info, dict) else {}
    listed = opencode("GET", f"/api/session/{session_id}/message")
    messages = _message_page(listed)
    return session.get("outcome"), assistant_text(messages), messages


def run_agent(text: str, images=None, session_key="", on_status=None) -> str:
    keys = [session_key] if isinstance(session_key, str) else list(session_key or [])
    keys = [item for item in keys if item]
    previous = ""
    existing = next((sessions.lookup(item) for item in keys if sessions.lookup(item)), "")
    if existing:
        try:
            _, previous, _messages = _session_reply(existing)
        except Exception as error:
            if keys and session_missing(error):
                sessions.drop(keys[0])
            previous = ""
    session_id = sessions.open(keys, _create_session)
    body = prompt_body(text, images or [])
    try:
        admitted = opencode("POST", f"/api/session/{session_id}/prompt", body, timeout=30)
    except Exception as error:
        if not (keys and session_missing(error)):
            raise
        sessions.drop(keys[0])
        session_id = sessions.open(keys, _create_session)
        admitted = opencode("POST", f"/api/session/{session_id}/prompt", body, timeout=30)
    user_id = ""
    if isinstance(admitted, dict):
        data = admitted.get("data") or admitted
        if isinstance(data, dict):
            user_id = data.get("id") or ""
    last = ""
    phase = "thinking"

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

    for _ in range(POLL_LIMIT):
        time.sleep(POLL_SECONDS)
        try:
            outcome, reply, messages = _session_reply(session_id)
        except Exception:
            continue
        fresh = reply_after(messages, user_id) if user_id else ""
        shown = fresh or reply
        beat = tool_beat(messages)
        if beat:
            note("tool", beat)
        elif shown and shown != previous and not outcome:
            note("write")
        if fresh and reply_ready("", fresh, outcome):
            return fresh[:12000]
        if reply_ready(previous, reply, outcome):
            return reply[:12000]
        if outcome and not reply and not previous:
            return f"OpenCode finished with outcome {outcome} and no text."
    return "OpenCode did not finish within 15 minutes."


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
    return _update(client, message_id, "interactive", json.dumps(card))


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
        summary = item.get("summary", "")
        shown = mr_flow.triage_card(summary, action_id) if left and mr_flow.is_triage(summary) else mr_flow.result_card(result)
        if message_id:
            try:
                update_card(client, message_id, shown)
            except Exception as error:
                print(f"card update failed {clean(str(error))}", flush=True)
        reply(client, message_id, result)

    threading.Thread(target=work, daemon=True).start()
    return _toast("opening draft MR", mr_flow.loading_card())


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

    return bundle(chosen, fetch)


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
            try:
                if posted["id"]:
                    update_text(client, posted["id"], line)
                else:
                    posted["id"], posted["thread_id"] = _send(
                        client, message_id, "text", json.dumps({"text": line}), in_thread=in_thread
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
                reply_card(client, posted["id"] or mid, mr_flow.triage_card(answer, action_id), in_thread=in_thread)

            dispatch(
                dedupe_id,
                message_id,
                text,
                lambda _mid, body: show(body),
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
