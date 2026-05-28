#!/usr/bin/env python3
"""Tiny local server for live editing and previewing the static demo page."""

from __future__ import annotations

import argparse
import html
import json
import os
import posixpath
import sys
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
EVENTS: set["LiveHandler"] = set()
EVENT_LOCK = threading.Lock()
LAST_SIGNATURE: dict[str, tuple[int, int]] = {}


LIVE_RELOAD_SNIPPET = """
<script>
(() => {
  if (window.__humanoidLiveReload) return;
  window.__humanoidLiveReload = true;
  const source = new EventSource('/__events');
  source.addEventListener('reload', () => window.location.reload());
})();
</script>
"""


EDITOR_FILE = "live-editor.html"
IGNORED_NAMES = {".git", "__pycache__"}


def resolve_inside_root(raw_path: str) -> Path:
    clean = unquote(raw_path).lstrip("/")
    target = (ROOT / clean).resolve()
    if ROOT != target and ROOT not in target.parents:
        raise ValueError("Path is outside the project directory.")
    if target.name in {Path(__file__).name, EDITOR_FILE}:
        raise ValueError("This utility file is read-only from the editor.")
    return target


def iter_watch_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in IGNORED_NAMES for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def signature() -> dict[str, tuple[int, int]]:
    current: dict[str, tuple[int, int]] = {}
    for path in iter_watch_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        current[str(path.relative_to(ROOT))] = (stat.st_mtime_ns, stat.st_size)
    return current


def notify_reload() -> None:
    with EVENT_LOCK:
        clients = list(EVENTS)
    for client in clients:
        try:
            client.wfile.write(b"event: reload\ndata: changed\n\n")
            client.wfile.flush()
        except OSError:
            with EVENT_LOCK:
                EVENTS.discard(client)


def watch_changes(interval: float) -> None:
    global LAST_SIGNATURE
    LAST_SIGNATURE = signature()
    while True:
        time.sleep(interval)
        current = signature()
        if current != LAST_SIGNATURE:
            LAST_SIGNATURE = current
            notify_reload()


def inject_live_reload(content: bytes) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    marker = "</body>"
    snippet = LIVE_RELOAD_SNIPPET
    if marker in text:
        text = text.replace(marker, snippet + "\n</body>", 1)
    else:
        text += snippet
    return text.encode("utf-8")


class LiveHandler(SimpleHTTPRequestHandler):
    server_version = "HumanoidLiveServer/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        path = posixpath.normpath(unquote(parsed.path))
        parts = [part for part in path.split("/") if part and part not in {".", ".."}]
        return str(ROOT.joinpath(*parts))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/__events":
            self.handle_events()
            return
        if parsed.path == "/__file":
            self.handle_read_file(parsed.query)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/__save":
            self.handle_save_file(parsed.query)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def handle_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        with EVENT_LOCK:
            EVENTS.add(self)
        try:
            while True:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(15)
        except OSError:
            pass
        finally:
            with EVENT_LOCK:
                EVENTS.discard(self)

    def handle_read_file(self, query: str) -> None:
        params = parse_qs(query)
        raw_path = params.get("path", ["index.html"])[0]
        try:
            target = resolve_inside_root(raw_path)
            data = target.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"ok": True, "path": str(target.relative_to(ROOT)), "content": data})

    def handle_save_file(self, query: str) -> None:
        params = parse_qs(query)
        raw_path = params.get("path", ["index.html"])[0]
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            content = payload["content"]
            if not isinstance(content, str):
                raise TypeError("content must be a string")
            target = resolve_inside_root(raw_path)
            target.write_text(content, encoding="utf-8")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        notify_reload()
        self.send_json({"ok": True, "path": str(target.relative_to(ROOT))})

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            for index in ("index.html", "index.htm"):
                candidate = os.path.join(path, index)
                if os.path.exists(candidate):
                    path = candidate
                    break
            else:
                return self.list_directory(path)

        ctype = self.guess_type(path)
        try:
            with open(path, "rb") as file:
                content = file.read()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        if ctype == "text/html" and Path(path).name != EDITOR_FILE:
            content = inject_live_reload(content)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        from io import BytesIO

        return BytesIO(content)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve this folder with live reload and an in-browser editor.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address, default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="Port, default: 8000")
    parser.add_argument("--interval", type=float, default=0.4, help="File watch interval in seconds")
    args = parser.parse_args()

    threading.Thread(target=watch_changes, args=(args.interval,), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), LiveHandler)
    url = f"http://{args.host}:{args.port}/"
    editor_url = f"http://{args.host}:{args.port}/{EDITOR_FILE}"
    print(f"Preview: {url}")
    print(f"Live editor: {editor_url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
