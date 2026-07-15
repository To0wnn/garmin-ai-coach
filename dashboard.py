#!/usr/bin/env python3
"""Read-only local-network dashboard for garmin-ai-coach: shows the latest
advice, advice history, core metric charts, recent activities, and an
adherence timeline (advice vs. what was actually done). Reuses coach.py's
existing InfluxDB query functions and coach_log — no separate query layer."""

import json
import os
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import coach

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8420"))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
COACH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coach.py")

# In-memory lock so a manual "run now" click can't overlap the daily cron run
# (or a second click) — both ultimately talk to the same single tmux/claude
# session, which can only handle one prompt at a time. dashboard.py already
# runs as the 'coach' user (see entrypoint.sh), same as cron_daily.sh's gosu
# step, so coach.py is invoked directly rather than via cron_daily.sh (which
# calls gosu itself and would fail — gosu requires root to switch user, and
# we're already the target user).
_run_lock = threading.Lock()
_run_state = {"running": False, "error": None}


def _run_coach_background():
    _run_state["running"] = True
    _run_state["error"] = None
    try:
        result = subprocess.run(["python3", COACH_SCRIPT], capture_output=True, text=True, timeout=330)
        if result.returncode != 0:
            stderr = result.stderr or result.stdout or "unknown error"
            if "already in progress" in stderr:
                _run_state["error"] = "Already running (e.g. the daily cron fired at the same time) — try again shortly."
            else:
                _run_state["error"] = stderr[-1000:]
    except Exception as e:
        _run_state["error"] = str(e)
    finally:
        _run_state["running"] = False


def build_payload() -> dict:
    history = coach.read_coach_log()
    metrics = coach.build_metrics()
    metrics["vo2max_series"] = coach.vo2max_series(28)
    return {
        "latest": history[-1] if history else None,
        "history": history,
        "metrics": metrics,
        "generated_at": datetime.now(coach.LOCAL_TZ).isoformat(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep container logs quiet — this is a low-traffic local page

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/data":
            self._serve_json()
        elif self.path == "/api/run-status":
            self._send_json({"running": _run_state["running"], "error": _run_state["error"]})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/run":
            self.send_error(404)
            return
        if _run_lock.locked() or _run_state["running"]:
            self._send_json({"started": False, "reason": "already running"}, status=409)
            return
        started = _run_lock.acquire(blocking=False)
        if not started:
            self._send_json({"started": False, "reason": "already running"}, status=409)
            return
        try:
            threading.Thread(target=self._run_and_release, daemon=True).start()
        except Exception:
            _run_lock.release()
            raise
        self._send_json({"started": True})

    def _run_and_release(self):
        try:
            _run_coach_background()
        finally:
            _run_lock.release()

    def _serve_html(self):
        with open(HTML_FILE, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self):
        try:
            payload = build_payload()
            status = 200
        except Exception as e:
            payload = {"error": str(e)}
            status = 502
        self._send_json(payload, status)

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", DASHBOARD_PORT), Handler)
    print(f"Dashboard listening on :{DASHBOARD_PORT}")
    server.serve_forever()
