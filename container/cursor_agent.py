"""Cursor Agent runtime for MoBox. One prompt is one local agent run."""

import json
import os
import subprocess
import uuid
from pathlib import Path

OPERATIONS = ["search", "branches", "file", "mrs", "mr", "diff", "pipelines", "trace", "issues", "commits", "crash"]
PROJECTS = ["gohost", "ios"]
_TRANSCRIPTS: dict[str, list] = {}


class MissingSession(RuntimeError):
    pass


def format_search(stdout: str) -> str:
    hits = json.loads(stdout)
    if not hits:
        return "No results."
    return json.dumps([
        {"path": hit["path"], "line": hit["startline"], "snippet": str(hit.get("data") or "")[:200]}
        for hit in hits[:10]
    ])


def format_file(stdout: str, start: int) -> str:
    lines = stdout.split("\n")
    excerpt = "\n".join(lines[start - 1:start + 79])[:5000]
    return f"Excerpt starting at line {start} of {len(lines)}\n{excerpt}"


def format_branches(stdout: str, project: str, requested: str) -> str:
    matches = [line for line in stdout.strip().split("\n") if line]
    if not matches:
        return "No matching branches."
    names = "\n".join(matches)
    if requested in matches:
        return f"Matching branches:\n{names}"
    return (
        f'No exact match for "{requested}" on {project}. Matching branch names:\n{names}\n\n'
        "If an exact branch was requested, report the missing branch and suggest these names in your reply. "
        "Ask which branch to use and end the lookup for this project until the user replies."
    )


def _runner(args: list[str]) -> str:
    binary = os.environ.get("MOBOX_GLAB_BIN", "/usr/local/bin/mobox-glab")
    env = {**os.environ, "GLAB_SEND_TELEMETRY": "false", "GLAB_CHECK_UPDATE": "false"}
    try:
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("The GitLab lookup timed out.") from error
    if completed.returncode != 0:
        reason = (completed.stderr or "").strip()[-1200:] or "The GitLab lookup failed."
        raise RuntimeError(reason)
    return completed.stdout


def mobox_gitlab(payload: dict, runner=None) -> str:
    run = runner or _runner
    operation = payload.get("operation")
    project = payload.get("project")
    args = payload.get("args") or []
    if operation not in OPERATIONS or project not in PROJECTS or not isinstance(args, list) or len(args) > 2:
        raise RuntimeError("Choose a supported GitLab operation and project.")
    args = [str(item) for item in args]
    stdout = run([operation, project, *args])
    if operation == "search":
        return format_search(stdout)
    if operation == "file":
        start = payload.get("start_line") or 1
        try:
            start = int(start)
        except (TypeError, ValueError):
            start = 1
        return format_file(stdout, max(1, start))
    if operation == "branches":
        return format_branches(stdout, project, args[0] if args else "")
    text = stdout.strip()
    return text[:5000] or "No results."


def _context_overflow(text: str) -> bool:
    low = (text or "").lower()
    marks = (
        "context length",
        "maximum context",
        "context window",
        "too many tokens",
        "token limit",
        "payload too large",
    )
    return any(mark in low for mark in marks)


def _images(payload: dict):
    from cursor_sdk import SDKImage

    images = []
    for item in payload.get("images") or []:
        data = item.get("data") or ""
        mime = item.get("mime") or "image/jpeg"
        if data:
            images.append(SDKImage.data_image(data, mime))
    return images


def _options(captured: list):
    from cursor_sdk import AgentOptions, CustomTool, LocalAgentOptions

    def execute(args, _context):
        tool_args = dict(args or {})
        try:
            output = mobox_gitlab(tool_args)
        except Exception as error:
            captured.append({"args": tool_args, "status": "error", "output": str(error)})
            raise
        captured.append({"args": tool_args, "status": "completed", "output": output})
        return output

    cwd = os.environ.get("CURSOR_AGENT_CWD", "/workspace")
    return AgentOptions(
        model=os.environ.get("CURSOR_MODEL", "grok-4.7"),
        api_key=os.environ.get("CURSOR_API_KEY") or "",
        name="MoBox",
        disallowed_tools=["shell", "edit", "task", "webSearch"],
        local=LocalAgentOptions(
            cwd=cwd,
            setting_sources=["project"],
            custom_tools={
                "mobox_gitlab": CustomTool(
                    description=(
                        "Run the read-only mobox-glab wrapper against GitLab. Use project gohost for Android and ios for iOS. "
                        "For branches pass [partial_branch] to find matching branch names on that project. "
                        "For search pass [branch, term]. For file pass [branch, path] and optionally set start_line for an excerpt. "
                        "For mr or diff pass [iid]. For mrs pass [state], pipelines [branch], trace [job_id], issues [label] or [], "
                        "commits [branch, path], and crash []. Repository paths are on GitLab."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string", "enum": OPERATIONS},
                            "project": {"type": "string", "enum": PROJECTS},
                            "args": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
                            "start_line": {"type": "integer", "minimum": 1, "description": "First line of a file excerpt. Defaults to 1."},
                        },
                        "required": ["operation", "project", "args"],
                        "additionalProperties": False,
                    },
                    execute=execute,
                ),
            },
        ),
    )


def _record(session_id: str, user_id: str, text: str, captured: list, reply: str, error: dict | None) -> list:
    items = _TRANSCRIPTS.setdefault(session_id, [])
    items.append({"type": "user", "id": user_id, "text": text})
    content = []
    for call in captured:
        content.append({
            "type": "tool",
            "name": "mobox_gitlab",
            "state": {
                "status": call["status"],
                "title": "mobox-glab " + str((call["args"] or {}).get("operation") or "lookup"),
                "input": call["args"],
                "content": [{"type": "text", "text": call["output"]}],
            },
        })
    if reply:
        content.append({"type": "text", "text": reply})
    assistant = {"type": "assistant", "content": content}
    if error:
        assistant["error"] = error
    items.append(assistant)
    return list(items)


def clear_transcripts() -> None:
    _TRANSCRIPTS.clear()


def run_prompt(payload: dict) -> dict:
    from cursor_sdk import Agent, CursorAgentError, UserMessage

    if not os.environ.get("CURSOR_API_KEY"):
        raise RuntimeError("CURSOR_API_KEY is empty")
    session_id = (payload.get("session_id") or "").strip()
    text = payload.get("text") or ""
    captured: list = []
    options = _options(captured)
    user_id = uuid.uuid4().hex
    started = False
    try:
        opener = Agent.resume(session_id, options) if session_id else Agent.create(options)
        with opener as agent:
            started = True
            images = _images(payload)
            message = UserMessage(text=text, images=images) if images else text
            run = agent.send(message)
            result = run.wait()
            session_id = agent.agent_id or session_id
            if not session_id:
                raise RuntimeError("cursor-agent did not return a session id")
    except CursorAgentError as error:
        message = str(error)
        if session_id and not started:
            raise MissingSession(message) from error
        raise RuntimeError(message) from error

    reply = result.result or ""
    failed = result.status == "error"
    error = None
    if failed and _context_overflow(reply):
        # Same shape the Lark bridge already retries once, in a fresh session.
        error = {"type": "provider.invalid-request", "status": 422}
    elif failed:
        error = {"type": "cursor-agent", "status": 500, "message": reply[:500]}
    messages = _record(session_id, user_id, text, captured, "" if failed else reply, error)
    workspace = Path(os.environ.get("CURSOR_AGENT_CWD", "/workspace"))
    print(f"cursor-agent session={session_id} status={result.status} cwd={workspace}", flush=True)
    return {
        "session_id": session_id,
        "user_id": user_id,
        "outcome": "failed" if failed else "finished",
        "reply": "" if failed else reply,
        "messages": messages,
        "error": error,
    }
