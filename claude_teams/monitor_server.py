"""
claude_teams.monitor_server
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tiny HTTP server (stdlib only) that serves the monitor dashboard UI and
pushes real-time state updates via Server-Sent Events (SSE).

Architecture
------------
* One background thread runs an HTTP server on port MONITOR_PORT (default 7373).
* GET /          → serves the embedded index.html
* GET /api/state → returns a full JSON snapshot of all teams + tasks + inboxes
* GET /api/events→ SSE stream; emits a "state" event whenever the ~/.claude/
                   filesystem changes (polled every POLL_INTERVAL seconds)

The server is started from app_lifespan in server.py and runs for the lifetime
of the MCP process.  It is intentionally dependency-free (stdlib only) so it
adds zero weight to the package.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MONITOR_PORT: int = int(os.environ.get("CLAUDE_TEAMS_MONITOR_PORT", "7373"))
POLL_INTERVAL: float = float(os.environ.get("CLAUDE_TEAMS_MONITOR_POLL", "2.0"))

CLAUDE_DIR = Path.home() / ".claude"
TEAMS_DIR = CLAUDE_DIR / "teams"
TASKS_DIR = CLAUDE_DIR / "tasks"

# ──────────────────────────────────────────────────────────────────────────────
# State snapshot builder
# ──────────────────────────────────────────────────────────────────────────────


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def build_state() -> dict:
    """Scan ~/.claude and return a complete state dict for the dashboard."""
    teams: list[dict] = []

    if not TEAMS_DIR.exists():
        return {"teams": teams, "generated_at": time.time()}

    for team_dir in sorted(TEAMS_DIR.iterdir()):
        if not team_dir.is_dir():
            continue
        config_path = team_dir / "config.json"
        if not config_path.exists():
            continue

        config = _read_json(config_path)
        if not isinstance(config, dict):
            continue

        team_name = config.get("name", team_dir.name)

        # Tasks
        tasks: list[dict] = []
        tasks_dir = TASKS_DIR / team_name
        if tasks_dir.exists():
            for tf in sorted(tasks_dir.glob("*.json"), key=lambda p: p.name):
                task = _read_json(tf)
                if isinstance(task, dict):
                    tasks.append(task)

        # Inboxes — collect all messages keyed by agent name
        inboxes: dict[str, list[dict]] = {}
        inboxes_dir = team_dir / "inboxes"
        if inboxes_dir.exists():
            for inbox_file in sorted(inboxes_dir.glob("*.json")):
                agent = inbox_file.stem
                msgs = _read_json(inbox_file)
                if isinstance(msgs, list):
                    inboxes[agent] = msgs

        teams.append(
            {
                "config": config,
                "tasks": tasks,
                "inboxes": inboxes,
            }
        )

    return {"teams": teams, "generated_at": time.time()}


# ──────────────────────────────────────────────────────────────────────────────
# SSE broadcaster
# ──────────────────────────────────────────────────────────────────────────────


class _SSEBroadcaster:
    """Thread-safe list of open SSE response queues."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[list] = []  # each client has its own queue list

    def subscribe(self) -> list:
        q: list = []
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: list) -> None:
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def broadcast(self, event: str, data: str) -> None:
        payload = f"event: {event}\ndata: {data}\n\n"
        with self._lock:
            for q in self._clients:
                q.append(payload)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


_broadcaster = _SSEBroadcaster()


# ──────────────────────────────────────────────────────────────────────────────
# Filesystem watcher thread
# ──────────────────────────────────────────────────────────────────────────────


def _mtime_fingerprint() -> float:
    """Sum of mtimes of all relevant files — cheap change detector."""
    total = 0.0
    for base in (TEAMS_DIR, TASKS_DIR):
        if not base.exists():
            continue
        for p in base.rglob("*.json"):
            try:
                total += p.stat().st_mtime
            except OSError:
                pass
    return total


def _watcher_thread(stop_event: threading.Event) -> None:
    last_fp = _mtime_fingerprint()
    while not stop_event.is_set():
        time.sleep(POLL_INTERVAL)
        fp = _mtime_fingerprint()
        if fp != last_fp:
            last_fp = fp
            if _broadcaster.client_count > 0:
                try:
                    state = build_state()
                    _broadcaster.broadcast("state", json.dumps(state))
                except Exception:
                    logger.exception("Error broadcasting state update")


# ──────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ──────────────────────────────────────────────────────────────────────────────


def _get_html() -> bytes:
    """Return the dashboard HTML."""
    here = Path(__file__).parent
    candidates = [
        here / "monitor" / "index.html",  # installed inside package
        here.parent / "monitor" / "index.html",  # running from fork repo root
    ]
    for p in candidates:
        if p.exists():
            return p.read_bytes()
    # Fallback minimal page
    return b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Claude Teams Monitor</title>
<style>body{background:#070b12;color:#00d4ff;font-family:monospace;padding:2rem;}
h1{color:#00d4ff;}p{color:#5a7a9a;}</style></head>
<body><h1>CLAUDE TEAMS MONITOR</h1>
<p>Dashboard HTML not found. Expected: monitor/index.html next to the package.</p>
<p><a href="/api/state" style="color:#00ff88;">/api/state</a> &mdash; raw JSON state</p>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # silence access log
        pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/state":
            self._serve_state()
        elif path == "/api/events":
            self._serve_sse()
        else:
            self.send_error(404)

    # ── route handlers ────────────────────────────────────────────────────────

    def _serve_html(self) -> None:
        body = _get_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_state(self) -> None:
        try:
            state = build_state()
            body = json.dumps(state).encode()
        except Exception as exc:
            body = json.dumps({"error": str(exc)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # Send an immediate full-state snapshot
        try:
            state = build_state()
            initial = f"event: state\ndata: {json.dumps(state)}\n\n".encode()
            self.wfile.write(initial)
            self.wfile.flush()
        except Exception:
            return

        q = _broadcaster.subscribe()
        try:
            while True:
                if q:
                    chunk = q.pop(0)
                    try:
                        self.wfile.write(chunk.encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                else:
                    # heartbeat comment every 15 s to keep connection alive
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    time.sleep(15)
        finally:
            _broadcaster.unsubscribe(q)


# ──────────────────────────────────────────────────────────────────────────────
# Public start / stop API
# ──────────────────────────────────────────────────────────────────────────────

_server_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_httpd: HTTPServer | None = None


def start(port: int = MONITOR_PORT) -> int:
    """Start the monitor HTTP server and filesystem watcher.

    Returns the port the server is listening on.
    Idempotent — calling again while running is a no-op.
    """
    global _server_thread, _stop_event, _httpd

    if _server_thread is not None and _server_thread.is_alive():
        return port

    _stop_event = threading.Event()

    try:
        _httpd = HTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        logger.warning("Claude Teams monitor could not bind to port %d: %s", port, exc)
        raise

    def _serve() -> None:
        assert _httpd is not None
        _httpd.serve_forever()

    _server_thread = threading.Thread(
        target=_serve, daemon=True, name="ct-monitor-http"
    )
    _server_thread.start()

    watcher = threading.Thread(
        target=_watcher_thread,
        args=(_stop_event,),
        daemon=True,
        name="ct-monitor-watcher",
    )
    watcher.start()

    logger.info("Claude Teams Monitor → http://localhost:%d", port)
    return port


def stop() -> None:
    """Gracefully stop the monitor server."""
    global _httpd, _stop_event
    if _stop_event:
        _stop_event.set()
    if _httpd:
        _httpd.shutdown()
        _httpd = None
