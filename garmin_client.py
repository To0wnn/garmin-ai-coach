#!/usr/bin/env python3
"""Wraps garminconnect.Garmin — owns the login/MFA state machine and token
persistence. No dashboard/HTTP knowledge here, pure library logic.

MFA gotchas (verified against garminconnect v0.3.6 source, not assumed):
- With return_on_mfa=True, MFA challenge state lives on the Garmin INSTANCE itself,
  not in the tuple login() returns — the same object must survive between the
  credential-submit step and the code-submit step (two separate web requests in
  dashboard.py's case), so PendingLogin holds onto it across calls.
- The return_on_mfa=True path skips the library's automatic token dump — dump_string()
  must be called manually after a successful login/resume_login(), or the session
  isn't persisted and the user has to MFA again on every restart.
"""

import os
import threading
import time

from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

import db

TOKEN_KEY = "garmin_token"
# An abandoned MFA attempt (user closes the tab mid-flow) shouldn't leak the
# in-progress Garmin instance forever — 10 min is generous for "open email, copy
# code, paste it" without keeping stale state around indefinitely.
PENDING_LOGIN_TIMEOUT_SECONDS = 600


class NotLoggedInError(Exception):
    pass


class PendingLogin:
    """Module-level singleton holding an in-progress (MFA-challenged) Garmin login
    attempt — mirrors session_manager.py's existing module-level-state convention
    for the AI-provider login flow."""

    def __init__(self):
        self._lock = threading.Lock()
        self._client: Garmin | None = None
        self._created_at: float = 0.0

    def set(self, client: Garmin):
        with self._lock:
            self._client = client
            self._created_at = time.time()

    def get(self) -> Garmin | None:
        with self._lock:
            if self._client is None:
                return None
            if time.time() - self._created_at > PENDING_LOGIN_TIMEOUT_SECONDS:
                self._client = None
                return None
            return self._client

    def clear(self):
        with self._lock:
            self._client = None
            self._created_at = 0.0


_pending = PendingLogin()


def _token_string() -> str | None:
    return db.get_sync_state(TOKEN_KEY)


def _save_token(client: Garmin):
    # client.client (the Garmin wrapper's own inner Client instance, confirmed via
    # direct inspection of garminconnect 0.3.6 — there is no client.garth attribute,
    # only client.client) .dumps() returns the token as a string — stored directly in
    # sync_state rather than a separate credential file on disk, keeping all Garmin
    # state in one place (the sqlite file already needs to exist and be backed up
    # regardless).
    db.set_sync_state(TOKEN_KEY, client.client.dumps())


def login_status() -> dict:
    """Cheap, local check — does NOT make a network call. A stored token existing
    doesn't guarantee it's still valid server-side; that's only discovered lazily on
    the first real API call (see get_client()'s NotLoggedInError contract)."""
    token = _token_string()
    return {"logged_in": token is not None}


def start_login(email: str, password: str) -> dict:
    """Returns {"needs_mfa": bool, "logged_in": bool, "error": str|None}."""
    client = Garmin(email=email, password=password, return_on_mfa=True)
    try:
        result1, result2 = client.login()
    except GarminConnectTooManyRequestsError:
        return {"needs_mfa": False, "logged_in": False, "error": "Garmin rate-limited this login — wait a while before retrying."}
    except GarminConnectAuthenticationError:
        return {"needs_mfa": False, "logged_in": False, "error": "Invalid email or password."}
    except Exception as e:
        return {"needs_mfa": False, "logged_in": False, "error": str(e)}

    if result1 == "needs_mfa":
        _pending.set(client)
        return {"needs_mfa": True, "logged_in": False, "error": None}

    _save_token(client)
    _pending.clear()
    return {"needs_mfa": False, "logged_in": True, "error": None}


def submit_mfa(code: str) -> dict:
    """Returns {"logged_in": bool, "error": str|None}. Keeps the pending login alive
    on a wrong code (within the timeout window) so the user can retry without
    restarting the whole email/password step."""
    client = _pending.get()
    if client is None:
        return {"logged_in": False, "error": "No login in progress (or it expired) — start again with email/password."}
    try:
        client.resume_login(None, code)
    except Exception as e:
        return {"logged_in": False, "error": f"MFA code not accepted: {e}"}

    _save_token(client)
    _pending.clear()
    return {"logged_in": True, "error": None}


def logout():
    db.execute("DELETE FROM sync_state WHERE key = ?", (TOKEN_KEY,))
    _pending.clear()


# Rate-limit pacing shared by all sync code (Stage 4's garmin_sync.py) — one place
# for the pacing logic rather than every caller reimplementing it. Login itself is
# NOT paced through this (see start_login above) — it should never be retried in a
# loop at all, paced or not, per the researched per-IP login rate limit.
_last_call_at = 0.0
_call_lock = threading.Lock()
_BACKOFF_START_SECONDS = 60
_BACKOFF_CAP_SECONDS = 900


def paced_call(fn, *args, min_interval: float = 1.5, **kwargs):
    global _last_call_at
    with _call_lock:
        wait = min_interval - (time.time() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.time()

    backoff = _BACKOFF_START_SECONDS
    while True:
        try:
            return fn(*args, **kwargs)
        except GarminConnectTooManyRequestsError:
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP_SECONDS)


def get_client() -> Garmin:
    """Returns a logged-in Garmin instance for sync code to use. Raises
    NotLoggedInError (a typed exception, not a bare Garmin one) if no token is
    stored, so callers can distinguish 'needs login' from a real API failure."""
    token = _token_string()
    if token is None:
        raise NotLoggedInError("No Garmin login — connect via the dashboard's Garmin settings.")
    client = Garmin(email=None, password=None)
    client.login(tokenstore=token)
    return client


if __name__ == "__main__":
    print(login_status())
