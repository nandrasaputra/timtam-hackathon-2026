#!/usr/bin/env python3
"""Local HTTP front for the MoBox Cursor Agent. Bound inside the container on port 4096."""

import json
import os
import shutil
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cursor_agent

_LOCK = threading.Lock()
_SW = """self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((key) => caches.delete(key)));
    await self.registration.unregister();
    const clients = await self.clients.matchAll({type: "window"});
    for (const client of clients) client.navigate(client.url);
  })());
});
"""


def request_path(raw: str) -> str:
    path = (raw or "").split("?", 1)[0]
    if path.startswith("http://") or path.startswith("https://"):
        path = urllib.parse.urlparse(path).path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/"


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MoBox</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 16px/1.4 ui-sans-serif, system-ui, sans-serif; background: #111; color: #eee; }
  main { max-width: 42rem; margin: 0 auto; padding: 1.5rem; }
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.25rem; }
  p { margin: 0 0 1rem; color: #aaa; }
  textarea { width: 100%; min-height: 8rem; box-sizing: border-box; padding: 0.75rem; border: 1px solid #333; border-radius: 8px; background: #1b1b1b; color: inherit; }
  button { margin-top: 0.75rem; padding: 0.5rem 0.9rem; border: 0; border-radius: 8px; background: #eee; color: #111; font: inherit; }
  button:disabled { opacity: 0.5; }
  pre { white-space: pre-wrap; padding: 0.75rem; background: #1b1b1b; border-radius: 8px; }
  .err { color: #f88; }
</style>
</head>
<body>
<main>
  <h1>MoBox</h1>
  <p>Cursor Agent. One thread stays on this page until you reload.</p>
  <form id="ask">
    <textarea name="text" required placeholder="Ask about the Android or iOS code"></textarea>
    <button type="submit">Send</button>
  </form>
  <pre id="out" hidden></pre>
</main>
<script>
const form = document.querySelector("#ask");
const out = document.querySelector("#out");
const button = form.querySelector("button");
let sessionId = "";
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = new FormData(form).get("text");
  button.disabled = true;
  out.hidden = false;
  out.className = "";
  out.textContent = "Running…";
  try {
    const response = await fetch("/v1/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || response.statusText);
    sessionId = payload.session_id || sessionId;
    out.textContent = payload.reply || payload.error || "No text.";
  } catch (error) {
    out.className = "err";
    out.textContent = error.message || String(error);
  } finally {
    button.disabled = false;
  }
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _route(self) -> str:
        return request_path(self.path)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Clear-Site-Data", '"cache", "storage"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self._route()
        if path in {"/", "/index.html"}:
            self._send(200, "text/html; charset=utf-8", _PAGE.encode())
            return
        if path == "/sw.js":
            self._send(200, "application/javascript; charset=utf-8", _SW.encode())
            return
        if path == "/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self._route() != "/v1/prompt":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "invalid json"})
            return
        try:
            with _LOCK:
                result = cursor_agent.run_prompt(payload)
        except cursor_agent.MissingSession as error:
            self._json(404, {"error": str(error)})
            return
        except Exception as error:
            print(f"cursor-agent failed: {error}", flush=True)
            self._json(500, {"error": str(error)})
            return
        self._json(200, result)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("cursor-agent " + (fmt % args), flush=True)


def main() -> None:
    if not os.environ.get("CURSOR_API_KEY"):
        sys.exit("CURSOR_API_KEY is empty")
    if shutil.which("agent") is None:
        sys.exit("cursor agent CLI is not on PATH")
    import cursor_sdk  # noqa: F401  # fail at startup when the SDK is missing
    port = int(os.environ.get("CURSOR_AGENT_PORT", "4096"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"cursor-agent listening on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
