#!/usr/bin/env python3
"""Read-only local-network dashboard for garmin-ai-coach: shows the latest
advice, advice history, core metric charts, recent activities, and an
adherence timeline (advice vs. what was actually done). Reuses coach.py's
existing InfluxDB query functions and coach_log — no separate query layer."""

import fcntl
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chat_ask
import coach
import session_manager
import settings as settings_module
from providers import PROVIDERS, get_provider

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

# Chat is a SEPARATE lock from _run_lock, not the same one — a chat conversation
# spans many turns over potentially minutes, while _run_lock is held only for one
# whole coach.py run. Reusing _run_lock for chat would let a long-idle chat panel
# block the daily cron indefinitely, which violates "cron/Run now always wins".
# Instead: _chat_lock only ever protects ONE turn at a time (held just for the
# duration of a single send_chat_message call), so cron/Run now only ever has to
# wait out a single short chat reply, never a whole conversation. Cross-process
# preemption against cron itself (which runs outside this process, via
# cron_daily.sh) is handled separately in chat_ask's use of coach.py's own
# /tmp/coach.lock — see _handle_chat_message below.
_chat_lock = threading.Lock()


def _try_coach_lock():
    """Non-blocking attempt at the SAME cross-process lock coach.py itself takes
    (see coach.py's LOCK_FILE/fcntl.flock in its __main__ block) — held only for
    the duration of one chat turn, then released immediately. This is what lets
    the daily cron run (a separate OS process, invoked via cron_daily.sh, outside
    this dashboard.py process entirely) safely preempt chat: cron's own flock
    attempt will succeed the moment a chat turn finishes and releases this file,
    without any explicit signal between the two processes. Returns an open file
    handle (caller must unlock+close it) on success, or None if coach.py is
    currently running."""
    fd = open(coach.LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


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
            # is_logged_in() checks each provider's own credential file directly
            # (~/.claude.json's oauthAccount, ~/.gemini/antigravity-cli's token file)
            # rather than the live tmux session — so it's safe to check every known
            # provider here, not just the currently active one. Lets the settings UI
            # show login status for both without the user having to switch the
            # active provider first just to see whether the other one is logged in.
            self._send_json({
                "provider": current,
                "logged_in": session_manager.is_logged_in(current),
                "session_alive": session_manager.is_session_alive(),
                "all": {name: session_manager.is_logged_in(name) for name in PROVIDERS},
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
        elif self.path == "/api/chat/start":
            self._handle_chat_start()
        elif self.path == "/api/chat/message":
            self._handle_chat_message()
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
            if not _chat_lock.acquire(blocking=False):
                self._send_json({"saved": False, "reason": "a chat message is in flight, try again shortly"}, status=409)
                return
            try:
                session_result = session_manager.start_session(updated["provider"])
            finally:
                _chat_lock.release()

        self._send_json({"saved": True, "settings": updated, "session": session_result})

    def _handle_login_start(self):
        body = self._read_json_body()
        active_provider = settings_module.read_settings()["provider"]
        target_provider = body.get("provider") or active_provider

        # Logging in to a provider that ISN'T the live tmux session requires
        # switching to it first — start_login() only works against the currently
        # running CLI's pane. Switching is the same guarded operation as a
        # settings-page provider change, so it needs the same _run_lock/_chat_lock
        # protection (see _handle_settings_update) before touching the session.
        if target_provider != active_provider:
            if _run_lock.locked() or _run_state["running"]:
                self._send_json({"url": None, "reason": "a run is in progress, try again shortly"}, status=409)
                return
            if not _chat_lock.acquire(blocking=False):
                self._send_json({"url": None, "reason": "a chat message is in flight, try again shortly"}, status=409)
                return
            try:
                session_manager.start_session(target_provider)
                settings_module.write_settings({"provider": target_provider})
            finally:
                _chat_lock.release()

        result = session_manager.start_login(target_provider)
        result["provider"] = target_provider
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
        body = self._read_json_body()
        active_provider = settings_module.read_settings()["provider"]
        # Which provider's credential to clear — defaults to the active one (the
        # only option before both providers' login status was shown side by side).
        # logout() itself only restarts the tmux session when target == active, so
        # logging out of the OTHER provider is a cheap credential-file-only op.
        target_provider = body.get("provider") or active_provider

        # session_manager.start_session() (kill-session + new-session + up to
        # READY_TIMEOUT_SECONDS of polling) only runs when target == active — only
        # need the run/chat guards in that case, otherwise a concurrent "Run
        # now"/chat turn/provider-switch can race it: two start_session() calls
        # stepping on each other (one's kill-session landing mid-poll of the
        # other's freshly started session) is what actually caused logout to need
        # a second click/Save to "unstick" before this guard existed.
        if target_provider == active_provider:
            if _run_lock.locked() or _run_state["running"]:
                self._send_json({"logged_out": False, "reason": "a run is in progress, try again shortly"}, status=409)
                return
            if not _chat_lock.acquire(blocking=False):
                self._send_json({"logged_out": False, "reason": "a chat message is in flight, try again shortly"}, status=409)
                return
            try:
                session_manager.logout(target_provider, active_provider)
            finally:
                _chat_lock.release()
        else:
            session_manager.logout(target_provider, active_provider)
        self._send_json({"logged_out": True, "provider": target_provider})

    def _handle_chat_start(self):
        if _run_lock.locked() or _run_state["running"]:
            self._send_json({"started": False, "reason": "a scheduled run is in progress, try again shortly"}, status=409)
            return
        if not _chat_lock.acquire(blocking=False):
            self._send_json({"started": False, "reason": "a chat message is already in flight"}, status=409)
            return
        try:
            lock_fd = _try_coach_lock()
            if lock_fd is None:
                self._send_json({"started": False, "reason": "a scheduled run is in progress, try again shortly"}, status=409)
                return
            try:
                chat_ask.start_chat()
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception as e:
            self._send_json({"started": False, "reason": str(e)}, status=502)
            return
        finally:
            _chat_lock.release()
        self._send_json({"started": True})

    def _handle_chat_message(self):
        body = self._read_json_body()
        message = (body.get("message") or "").strip()
        first = bool(body.get("first"))
        if not message:
            self._send_json({"error": "no message provided"}, status=400)
            return

        if _run_lock.locked() or _run_state["running"]:
            self._send_json({"reply": None, "paused": True, "reason": "a scheduled run is in progress"}, status=409)
            return
        if not _chat_lock.acquire(blocking=False):
            self._send_json({"reply": None, "paused": False, "reason": "a chat message is already in flight"}, status=409)
            return
        try:
            lock_fd = _try_coach_lock()
            if lock_fd is None:
                self._send_json({"reply": None, "paused": True, "reason": "a scheduled run is in progress"}, status=409)
                return
            try:
                prompt = f"{coach.build_chat_context()}\n\nThe user asks: {message}" if first else message
                provider = settings_module.read_settings()["provider"]
                write_tool_name = get_provider(provider)["write_tool_name"]
                reply = chat_ask.send_chat_message(prompt, write_tool_name)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception as e:
            self._send_json({"reply": None, "paused": False, "reason": str(e)}, status=502)
            return
        finally:
            _chat_lock.release()
        self._send_json({"reply": reply, "paused": False})

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
