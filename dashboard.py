#!/usr/bin/env python3
"""Read-only local-network dashboard for garmin-ai-coach: shows the latest
advice, advice history, core metric charts, recent activities, and an
adherence timeline (advice vs. what was actually done). Uses coach_sqlite.py's
SQLite-backed query functions and coach_log — see coach_sqlite.py's own
docstring for why the InfluxDB-backed coach.py is still imported alongside it
(shared prompt/Discord/locking logic, and coach.py's own InfluxDB path is kept
running as a fallback net during the Stage 6 soak period)."""

import fcntl
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import chat_ask
import coach
import coach_sqlite
import db
import garmin_client
import garmin_sync
import session_manager
import settings as settings_module
from providers import PROVIDERS, get_provider

# TEMPORARY: every handler below acts as user_id=1 (the pre-existing single
# user) until Stage 6 wires real per-request auth (session cookie -> current
# user) through this file — see the multi-user plan. Centralized here as one
# function so Stage 6's actual change is swapping this body for a cookie
# lookup, not hunting down every call site again.
def _current_user_id_TEMP() -> int:
    return 1


def _effective_owner_id(user_id: int) -> int:
    """The AI-session owner this user's prompts should run through — their
    own session by default, or a borrowed owner's if they've redeemed a
    share code (see auth.py's session_owner_id column/redeem_share_code)."""
    return auth.session_owner_id_of(auth.get_user_by_id(user_id))


def _session_owner_label(user: dict) -> dict:
    """For the settings UI: "Your own AI session" or "Borrowing <username>'s
    session", plus the raw owner_id so the frontend can show a "stop
    borrowing" action only when it's actually borrowed."""
    owner_id = auth.session_owner_id_of(user)
    if owner_id == user["id"]:
        return {"owner_id": owner_id, "borrowed": False, "owner_username": None}
    owner = auth.get_user_by_id(owner_id)
    return {"owner_id": owner_id, "borrowed": True, "owner_username": owner["username"] if owner else None}


DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8420"))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
# Cutover: the dashboard's "Run now" button and build_payload() now drive the
# SQLite-backed pipeline (coach_sqlite.py) instead of coach.py's InfluxDB one —
# coach.py itself, and the garmin-grafana/InfluxDB stack it reads from, are
# deliberately left running untouched as a fallback net during the Stage 6
# soak period (see project plan/memory), not actively used by the dashboard
# anymore. Reverting is a one-line change back to coach.py if needed.
COACH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coach_sqlite.py")

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

# Backfill is its own lock, separate from _run_lock/_chat_lock — it's a long,
# multi-day-history fetch running in its own background thread, unrelated to the
# AI-provider tmux session those two locks protect. Keyed per USER (not per
# AI-session-owner like _run_lock/_chat_lock) since backfill is purely a
# Garmin-data-sync concern — two different users' Garmin accounts backfilling
# at the same time must not block each other, even if they happen to share
# one AI session. Each user's sync loop cycle checks that SAME user's
# backfill lock before writing (see _sync_once) so a running backfill for
# user A never collides with the ongoing sync writing user A's own recent-day
# rows, while user B's sync/backfill proceeds independently.
_backfill_locks: dict[int, threading.Lock] = {}
_backfill_states: dict[int, dict] = {}
_backfill_dict_lock = threading.Lock()  # guards insertion into the two dicts above only


def _backfill_lock_for(user_id: int) -> threading.Lock:
    with _backfill_dict_lock:
        if user_id not in _backfill_locks:
            _backfill_locks[user_id] = threading.Lock()
            _backfill_states[user_id] = {"running": False, "error": None}
        return _backfill_locks[user_id]


def _backfill_state_for(user_id: int) -> dict:
    _backfill_lock_for(user_id)  # ensures the state dict exists too (created together above)
    return _backfill_states[user_id]


def _try_coach_lock(owner_id: int):
    """Non-blocking attempt at the SAME cross-process lock coach.py itself takes
    for this AI-session owner (see coach.owner_lock_file()/fcntl.flock in
    coach_sqlite.py's __main__ block) — held only for the duration of one chat
    turn, then released immediately. This is what lets the daily cron run (a
    separate OS process, invoked via cron_daily.sh, outside this dashboard.py
    process entirely) safely preempt chat: cron's own flock attempt will
    succeed the moment a chat turn finishes and releases this file, without
    any explicit signal between the two processes. Returns an open file
    handle (caller must unlock+close it) on success, or None if a run is
    currently in progress for this owner's session."""
    fd = open(coach.owner_lock_file(owner_id), "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def _run_backfill_background(user_id: int, start_date: str, end_date: str):
    state = _backfill_state_for(user_id)
    state["running"] = True
    state["error"] = None
    try:
        garmin_sync.run_backfill(user_id, start_date, end_date)
    except garmin_client.NotLoggedInError as e:
        state["error"] = str(e)
    except Exception as e:
        state["error"] = str(e)
    finally:
        state["running"] = False


# Ongoing sync: an in-process scheduler thread rather than a new cron entry.
# This project has TWO documented real cron incidents already (a missing PATH=
# in /etc/cron.d/coach requiring an explicit export, and a stuck cross-process
# flock from a fragmented tmux paste) — a thread inside the already-correctly-
# configured dashboard.py process sidesteps cron's environment-inheritance
# problems entirely and needs no new .env.runtime entry.
SYNC_INTERVAL_SECONDS = 300


def _sync_once(user_id: int):
    # TEMPORARY: syncs only user_id's account — Stage 10 makes this loop over
    # every registered user (each with their own Garmin login), not just one.
    if _backfill_lock_for(user_id).locked():
        return  # let the (rarer, longer) backfill have the write path this cycle
    try:
        client = garmin_client.get_client(user_id)
    except garmin_client.NotLoggedInError:
        return
    except Exception:
        return
    try:
        today = datetime.now(coach.LOCAL_TZ).date()
        garmin_sync.sync_day(user_id, client, today.isoformat(), intraday=True)
        # Also re-sync yesterday — catches a watch sync that finishes
        # processing after local midnight (the same concern
        # wait_for_fresh_sync() used to guess at with a bounded sleep;
        # here we just re-pull instead of waiting and hoping).
        garmin_sync.sync_day(user_id, client, (today - timedelta(days=1)).isoformat(), intraday=True)
        db.set_sync_state(user_id, "last_sync_at", datetime.now(coach.LOCAL_TZ).isoformat())
    except Exception:
        pass  # best-effort — next cycle tries again, no crash-loop


def _sync_loop():
    # Sync immediately on startup (not just after the first 5-minute wait) — every
    # dashboard.py restart (deploy, crash-recovery) should catch up right away
    # rather than leaving the dashboard showing stale data for up to 5 minutes.
    user_id = _current_user_id_TEMP()
    _sync_once(user_id)
    while True:
        time.sleep(SYNC_INTERVAL_SECONDS)
        _sync_once(user_id)


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


def build_payload(user_id: int) -> dict:
    history = coach_sqlite.read_coach_log(user_id)
    metrics = coach_sqlite.build_metrics(user_id)
    metrics["vo2max_series"] = coach_sqlite.vo2max_series(user_id, 28)
    return {
        "latest": history[-1] if history else None,
        "history": history,
        "metrics": metrics,
        "generated_at": datetime.now(coach.LOCAL_TZ).isoformat(),
    }


# Routes reachable without a valid session cookie — everything else in
# do_GET/do_POST requires self.current_user to be set first.
_PUBLIC_GET_PATHS = {"/login"}
_PUBLIC_POST_PATHS = {"/api/dashboard-login", "/api/register"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep container logs quiet — this is a low-traffic local page

    def _current_user(self) -> dict | None:
        """Reads the session cookie (if any) and resolves it to a user row via
        auth.py. Cached per-request on self so repeated access within one
        handler doesn't re-hit the DB."""
        if hasattr(self, "_current_user_cache"):
            return self._current_user_cache
        cookie_header = self.headers.get("Cookie", "")
        token = None
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("session="):
                token = part[len("session="):]
                break
        user = auth.get_user_by_session(token) if token else None
        self._current_user_cache = user
        return user

    def _require_user(self) -> dict | None:
        """Sends a 401 and returns None if there's no valid session — callers
        do `user = self._require_user(); if user is None: return`."""
        user = self._current_user()
        if user is None:
            self._send_json({"error": "not logged in"}, status=401)
            return None
        return user

    def do_GET(self):
        # /register/<token> is public (account-setup page for an invitee) but
        # is a prefix match, not an exact path, so it's checked separately
        # from the exact-match _PUBLIC_GET_PATHS set.
        is_public = self.path in _PUBLIC_GET_PATHS or self.path.startswith("/register/")
        if not is_public and self._current_user() is None and self.path not in ("/api/whoami",):
            if self.path == "/" or self.path == "/settings":
                self._serve_html()  # dashboard.html itself renders a login form client-side
                return
            self._send_json({"error": "not logged in"}, status=401)
            return

        if self.path == "/" or self.path == "/login" or self.path == "/settings" or self.path.startswith("/register/"):
            self._serve_html()
        elif self.path == "/api/whoami":
            user = self._current_user()
            self._send_json({"logged_in": user is not None, "username": user["username"] if user else None, "is_admin": bool(user["is_admin"]) if user else False})
        elif self.path == "/api/data":
            self._serve_json()
        elif self.path == "/api/run-status":
            self._send_json({"running": _run_state["running"], "error": _run_state["error"]})
        elif self.path == "/api/settings":
            user = self.current_user
            s = settings_module.read_settings(user["id"])
            s["providers"] = {k: v["label"] for k, v in PROVIDERS.items()}
            s["session_owner"] = _session_owner_label(user)
            self._send_json(s)
        elif self.path == "/api/auth-status":
            user = self.current_user
            owner_id = _effective_owner_id(user["id"])
            current = settings_module.read_settings(user["id"])["provider"]
            # is_logged_in() checks each provider's own credential file directly
            # (~/.claude.json's oauthAccount, ~/.gemini/antigravity-cli's token file)
            # rather than the live tmux session — so it's safe to check every known
            # provider here, not just the currently active one. Lets the settings UI
            # show login status for both without the user having to switch the
            # active provider first just to see whether the other one is logged in.
            self._send_json({
                "provider": current,
                "logged_in": session_manager.is_logged_in(owner_id, current),
                "session_alive": session_manager.is_session_alive(owner_id),
                "all": {name: session_manager.is_logged_in(owner_id, name) for name in PROVIDERS},
            })
        elif self.path == "/api/garmin/status":
            user_id = self.current_user["id"]
            status = garmin_client.login_status(user_id)
            self._send_json({
                "logged_in": status["logged_in"],
                "last_sync_at": db.get_sync_state(user_id, "last_sync_at"),
                "backfill": db.get_sync_state(user_id, garmin_sync.BACKFILL_PROGRESS_KEY),
            })
        elif self.path == "/api/garmin/backfill-status":
            user_id = self.current_user["id"]
            state = _backfill_state_for(user_id)
            self._send_json({
                "running": state["running"],
                "error": state["error"],
                "progress": db.get_sync_state(user_id, garmin_sync.BACKFILL_PROGRESS_KEY),
            })
        elif self.path == "/api/admin/users":
            if not self.current_user["is_admin"]:
                self._send_json({"error": "admin only"}, status=403)
                return
            users = [
                {"id": u["id"], "username": u["username"], "is_admin": bool(u["is_admin"]), "created_at": u["created_at"]}
                for u in auth.list_users()
            ]
            self._send_json({"users": users})
        else:
            self.send_error(404)

    def do_POST(self):
        is_public = self.path in _PUBLIC_POST_PATHS
        if not is_public and self._current_user() is None:
            self._send_json({"error": "not logged in"}, status=401)
            return

        if self.path == "/api/dashboard-login":
            self._handle_dashboard_login()
        elif self.path == "/api/register":
            self._handle_register()
        elif self.path == "/api/dashboard-logout":
            self._handle_dashboard_logout()
        elif self.path == "/api/change-password":
            self._handle_change_password()
        elif self.path == "/api/run":
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
        elif self.path == "/api/garmin/login/start":
            self._handle_garmin_login_start()
        elif self.path == "/api/garmin/login/mfa":
            self._handle_garmin_login_mfa()
        elif self.path == "/api/garmin/logout":
            self._handle_garmin_logout()
        elif self.path == "/api/garmin/backfill/start":
            self._handle_garmin_backfill_start()
        elif self.path == "/api/admin/invite":
            self._handle_admin_invite()
        elif self.path == "/api/share/create":
            self._handle_share_create()
        elif self.path == "/api/share/redeem":
            self._handle_share_redeem()
        elif self.path == "/api/share/revoke":
            self._handle_share_revoke()
        else:
            self.send_error(404)

    @property
    def current_user(self) -> dict:
        """Non-None accessor for handlers reached only after a _require_user()/
        do_GET/do_POST guard already confirmed a session exists — avoids
        repeating a None-check in every single handler body."""
        return self._current_user()

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
        user_id = self.current_user["id"]
        before = settings_module.read_settings(user_id)
        updated = settings_module.write_settings(user_id, body)

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
                owner_id = _effective_owner_id(user_id)
                session_result = session_manager.start_session(owner_id, updated["provider"])
            finally:
                _chat_lock.release()

        self._send_json({"saved": True, "settings": updated, "session": session_result})

    def _handle_login_start(self):
        body = self._read_json_body()
        user_id = self.current_user["id"]
        owner_id = _effective_owner_id(user_id)
        active_provider = settings_module.read_settings(user_id)["provider"]
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
                session_manager.start_session(owner_id, target_provider)
                settings_module.write_settings(user_id, {"provider": target_provider})
            finally:
                _chat_lock.release()

        result = session_manager.start_login(owner_id, target_provider)
        result["provider"] = target_provider
        self._send_json(result)

    def _handle_login_code(self):
        body = self._read_json_body()
        owner_id = _effective_owner_id(self.current_user["id"])
        code = body.get("code", "").strip()
        if not code:
            self._send_json({"error": "no code provided"}, status=400)
            return
        session_manager.submit_login_code(owner_id, code)
        time.sleep(3)
        provider = settings_module.read_settings(self.current_user["id"])["provider"]
        self._send_json({"logged_in": session_manager.is_logged_in(owner_id, provider)})

    def _handle_logout(self):
        body = self._read_json_body()
        user_id = self.current_user["id"]
        owner_id = _effective_owner_id(user_id)
        active_provider = settings_module.read_settings(user_id)["provider"]
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
                session_manager.logout(owner_id, target_provider, active_provider)
            finally:
                _chat_lock.release()
        else:
            session_manager.logout(owner_id, target_provider, active_provider)
        self._send_json({"logged_out": True, "provider": target_provider})

    def _handle_chat_start(self):
        owner_id = _effective_owner_id(self.current_user["id"])
        if _run_lock.locked() or _run_state["running"]:
            self._send_json({"started": False, "reason": "a scheduled run is in progress, try again shortly"}, status=409)
            return
        if not _chat_lock.acquire(blocking=False):
            self._send_json({"started": False, "reason": "a chat message is already in flight"}, status=409)
            return
        try:
            lock_fd = _try_coach_lock(owner_id)
            if lock_fd is None:
                self._send_json({"started": False, "reason": "a scheduled run is in progress, try again shortly"}, status=409)
                return
            try:
                chat_ask.start_chat(owner_id)
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

        user_id = self.current_user["id"]
        owner_id = _effective_owner_id(user_id)
        if _run_lock.locked() or _run_state["running"]:
            self._send_json({"reply": None, "paused": True, "reason": "a scheduled run is in progress"}, status=409)
            return
        if not _chat_lock.acquire(blocking=False):
            self._send_json({"reply": None, "paused": False, "reason": "a chat message is already in flight"}, status=409)
            return
        try:
            lock_fd = _try_coach_lock(owner_id)
            if lock_fd is None:
                self._send_json({"reply": None, "paused": True, "reason": "a scheduled run is in progress"}, status=409)
                return
            try:
                prompt = f"{coach_sqlite.build_chat_context(user_id)}\n\nThe user asks: {message}" if first else message
                provider = settings_module.read_settings(user_id)["provider"]
                write_tool_name = get_provider(provider)["write_tool_name"]
                reply = chat_ask.send_chat_message(owner_id, prompt, write_tool_name)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception as e:
            self._send_json({"reply": None, "paused": False, "reason": str(e)}, status=502)
            return
        finally:
            _chat_lock.release()
        self._send_json({"reply": reply, "paused": False})

    def _handle_garmin_login_start(self):
        body = self._read_json_body()
        user_id = self.current_user["id"]
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or not password:
            self._send_json({"error": "email and password required"}, status=400)
            return
        result = garmin_client.start_login(user_id, email, password)
        status = 200 if result["error"] is None else 400
        self._send_json(result, status)

    def _handle_garmin_login_mfa(self):
        body = self._read_json_body()
        user_id = self.current_user["id"]
        code = (body.get("code") or "").strip()
        if not code:
            self._send_json({"error": "no code provided"}, status=400)
            return
        result = garmin_client.submit_mfa(user_id, code)
        status = 200 if result["logged_in"] else 400
        self._send_json(result, status)

    def _handle_garmin_logout(self):
        garmin_client.logout(self.current_user["id"])
        self._send_json({"logged_out": True})

    def _handle_garmin_backfill_start(self):
        body = self._read_json_body()
        user_id = self.current_user["id"]
        start_date = body.get("start_date")
        end_date = body.get("end_date")
        if not start_date or not end_date:
            self._send_json({"started": False, "reason": "start_date and end_date required"}, status=400)
            return
        if not garmin_client.login_status(user_id)["logged_in"]:
            self._send_json({"started": False, "reason": "not logged in to Garmin"}, status=400)
            return
        lock = _backfill_lock_for(user_id)
        state = _backfill_state_for(user_id)
        if lock.locked() or state["running"]:
            self._send_json({"started": False, "reason": "a backfill is already running"}, status=409)
            return
        if not lock.acquire(blocking=False):
            self._send_json({"started": False, "reason": "a backfill is already running"}, status=409)
            return
        try:
            threading.Thread(
                target=self._run_backfill_and_release, args=(user_id, start_date, end_date), daemon=True
            ).start()
        except Exception:
            lock.release()
            raise
        self._send_json({"started": True})

    def _run_backfill_and_release(self, user_id, start_date, end_date):
        try:
            _run_backfill_background(user_id, start_date, end_date)
        finally:
            _backfill_lock_for(user_id).release()

    def _set_session_cookie(self, token: str):
        # No Secure flag — confirmed local-network-only deployment (no forced
        # HTTPS in front of this dashboard), see the multi-user plan.
        self.send_header(
            "Set-Cookie",
            f"session={token}; HttpOnly; SameSite=Lax; Max-Age={int(auth.SESSION_TTL.total_seconds())}; Path=/",
        )

    def _clear_session_cookie(self):
        self.send_header("Set-Cookie", "session=; HttpOnly; SameSite=Lax; Max-Age=0; Path=/")

    def _handle_dashboard_login(self):
        body = self._read_json_body()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        user = auth.authenticate(username, password)
        if user is None:
            self._send_json({"logged_in": False, "reason": "invalid username or password"}, status=401)
            return
        token = auth.create_session(user["id"])
        body_bytes = json.dumps({"logged_in": True, "username": user["username"]}).encode()
        self.send_response(200)
        self._set_session_cookie(token)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _handle_dashboard_logout(self):
        cookie_header = self.headers.get("Cookie", "")
        token = None
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("session="):
                token = part[len("session="):]
                break
        if token:
            auth.delete_session(token)
        body_bytes = json.dumps({"logged_out": True}).encode()
        self.send_response(200)
        self._clear_session_cookie()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _handle_change_password(self):
        body = self._read_json_body()
        current_password = body.get("current_password") or ""
        new_password = body.get("new_password") or ""
        if not current_password or not new_password:
            self._send_json({"changed": False, "reason": "current_password and new_password required"}, status=400)
            return
        if len(new_password) < 8:
            self._send_json({"changed": False, "reason": "new password must be at least 8 characters"}, status=400)
            return
        ok = auth.change_password(self.current_user["id"], current_password, new_password)
        if not ok:
            self._send_json({"changed": False, "reason": "current password is incorrect"}, status=400)
            return
        self._send_json({"changed": True})

    def _handle_register(self):
        body = self._read_json_body()
        token = (body.get("invite_token") or "").strip()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not token or not username or not password:
            self._send_json({"registered": False, "reason": "invite_token, username, and password required"}, status=400)
            return
        user = auth.redeem_invite(token, username, password)
        if user is None:
            self._send_json({"registered": False, "reason": "invite link is invalid, expired, or already used"}, status=400)
            return
        session_token = auth.create_session(user["id"])
        body_bytes = json.dumps({"registered": True, "username": user["username"]}).encode()
        self.send_response(200)
        self._set_session_cookie(session_token)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _handle_admin_invite(self):
        user = self.current_user
        if not user["is_admin"]:
            self._send_json({"error": "admin only"}, status=403)
            return
        token = auth.create_invite(user["id"])
        self._send_json({"invite_token": token, "register_path": f"/register/{token}"})

    def _handle_share_create(self):
        body = self._read_json_body()
        label = (body.get("label") or "").strip()
        code = auth.create_share_code(self.current_user["id"], label=label)
        self._send_json({"code": code, "label": label})

    def _handle_share_redeem(self):
        body = self._read_json_body()
        code = (body.get("code") or "").strip()
        if not code:
            self._send_json({"redeemed": False, "reason": "no code provided"}, status=400)
            return
        share = auth.redeem_share_code(self.current_user["id"], code)
        if share is None:
            self._send_json({"redeemed": False, "reason": "code is invalid or revoked"}, status=400)
            return
        owner = auth.get_user_by_id(share["owner_user_id"])
        self._send_json({"redeemed": True, "owner_username": owner["username"] if owner else None})

    def _handle_share_revoke(self):
        body = self._read_json_body()
        code = (body.get("code") or "").strip()
        ok = auth.revoke_share_code(self.current_user["id"], code)
        if not ok:
            self._send_json({"revoked": False, "reason": "code not found or not owned by you"}, status=400)
            return
        self._send_json({"revoked": True})

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
            payload = build_payload(self.current_user["id"])
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
    db.init_schema()
    threading.Thread(target=_sync_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", DASHBOARD_PORT), Handler)
    print(f"Dashboard listening on :{DASHBOARD_PORT}")
    server.serve_forever()
