#!/usr/bin/env python3
"""Read-only local-network dashboard for garmin-ai-coach: shows the latest
advice, advice history, core metric charts, recent activities, and an
adherence timeline (advice vs. what was actually done). Reuses coach.py's
existing InfluxDB query functions and coach_log — no separate query layer."""

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import coach
import session_manager
import settings as settings_module
from providers import PROVIDERS

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8420"))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
COACH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coach.py")

# In-memory lock so a manual "run now" click can't overlap the daily cron run
# (or a second click) — both ultimately talk to the same single tmux/claude
# session, which can only handle one prompt at a time. dashboard.py already
# runs as the 'coach' user (see entrypoint.sh), same as cron_daily.sh's gosu
# step, so coach.py is invoked directly rather than via cron_daily.sh (which
# calls gosu itself and would fail — gosu requires root to switch user, and
# we're already the target user). Also guards against a provider switch racing
# an in-flight run — switching tears down the tmux session a run might be
# mid-conversation with.
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
        elif self.path == "/api/settings":
            s = settings_module.read_settings()
            s["providers"] = {k: v["label"] for k, v in PROVIDERS.items()}
            self._send_json(s)
        elif self.path == "/api/auth-status":
            current = settings_module.read_settings()["provider"]
            self._send_json({
                "provider": current,
                "logged_in": session_manager.is_logged_in(current),
                "session_alive": session_manager.is_session_alive(),
            })
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/run":
            self._handle_run()
        elif self.path == "/api/settings":
            self._handle_settings_update()
        elif self.path == "/api/login/start":
            self._handle_login_start()
        elif self.path == "/api/login/code":
            self._handle_login_code()
        elif self.path == "/api/logout":
            self._handle_logout()
        else:
            self.send_error(404)

    def _handle_run(self):
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

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _handle_settings_update(self):
        body = self._read_json_body()
        before = settings_module.read_settings()
        updated = settings_module.write_settings(body)

        provider_changed = "provider" in body and body["provider"] != before["provider"]
        session_result = None
        if provider_changed:
            if _run_lock.locked() or _run_state["running"]:
                self._send_json({"saved": False, "reason": "a run is in progress, try again shortly"}, status=409)
                return
            session_result = session_manager.start_session(updated["provider"])

        self._send_json({"saved": True, "settings": updated, "session": session_result})

    def _handle_login_start(self):
        provider = settings_module.read_settings()["provider"]
        result = session_manager.start_login(provider)
        self._send_json(result)

    def _handle_login_code(self):
        body = self._read_json_body()
        code = body.get("code", "").strip()
        if not code:
            self._send_json({"error": "no code provided"}, status=400)
            return
        session_manager.submit_login_code(code)
        time.sleep(3)
        provider = settings_module.read_settings()["provider"]
        self._send_json({"logged_in": session_manager.is_logged_in(provider)})

    def _handle_logout(self):
        if _run_lock.locked() or _run_state["running"]:
            self._send_json({"logged_out": False, "reason": "a run is in progress, try again shortly"}, status=409)
            return
        provider = settings_module.read_settings()["provider"]
        session_manager.logout(provider)
        self._send_json({"logged_out": True})

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
