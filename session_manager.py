#!/usr/bin/env python3
"""Starts/stops the permanent 'coach' tmux session that runs whichever AI CLI
is currently selected (see providers.py). Used both by entrypoint.sh at
container startup and by dashboard.py when the user switches providers on
the settings page — the same logic either way, so it isn't duplicated as a
bash loop (startup) and a Python loop (dashboard) like the earlier
Claude-only version had."""

import os
import re
import subprocess
import tempfile
import time

from providers import get_provider

SESSION = "coach"
TMUX_TMPDIR = os.environ.get("TMUX_TMPDIR", "/tmp/tmux-shared")
READY_TIMEOUT_SECONDS = 30
READY_POLL_SECONDS = 1


def _tmux(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TMUX_TMPDIR": TMUX_TMPDIR}
    return subprocess.run(["tmux", *args], capture_output=True, text=True, env=env)


def is_session_alive() -> bool:
    return _tmux("has-session", "-t", SESSION).returncode == 0


def stop_session():
    if is_session_alive():
        _tmux("kill-session", "-t", SESSION)


def _pane_text() -> str:
    result = _tmux("capture-pane", "-t", SESSION, "-p")
    return result.stdout if result.returncode == 0 else ""


def start_session(provider_name: str) -> dict:
    """Kills any existing session, starts a fresh one for the given provider,
    and waits (bounded) for it to reach a ready or login-needed state.
    Returns {"ready": bool, "needs_login": bool}."""
    provider = get_provider(provider_name)
    stop_session()

    # entrypoint.sh creates TMUX_TMPDIR with the right permissions (1777) as
    # root before dropping to the 'coach' user — this call itself always runs
    # as 'coach' (both at startup and from the dashboard), so it can't chmod a
    # root-owned directory. Only handle the directory-doesn't-exist case (e.g.
    # a future caller that isn't entrypoint.sh).
    if not os.path.isdir(TMUX_TMPDIR):
        os.makedirs(TMUX_TMPDIR, exist_ok=True)

    _tmux("new-session", "-d", "-s", SESSION, "-x", "220", "-y", "50", provider["start_cmd"])

    deadline = time.time() + READY_TIMEOUT_SECONDS
    saw_login_screen = False
    while time.time() < deadline:
        pane = _pane_text().lower()
        if provider["ready_marker"].lower() in pane:
            return {"ready": True, "needs_login": False}
        if provider["login_screen_marker"].lower() in pane:
            # Don't return immediately — an already-logged-in session can
            # transiently show an onboarding/login-method screen for a moment
            # before landing on the ready prompt (observed with Antigravity
            # CLI when credentials are already cached). Keep polling; only
            # report needs_login if the pane is STILL on that screen once the
            # loop times out below.
            saw_login_screen = True
        else:
            saw_login_screen = False
        time.sleep(READY_POLL_SECONDS)

    return {"ready": False, "needs_login": saw_login_screen}


# tmux wraps long lines (e.g. OAuth URLs with long query strings) at the pane
# width, inserting a newline mid-URL — capture-pane's plain-text output has no
# soft-wrap marker to undo that automatically. Strip whitespace/newlines out of
# the whole pane text before matching so a wrapped URL still matches as one
# token (confirmed necessary: Antigravity CLI's OAuth URL wraps across 3 lines
# in a 220-column pane).
_URL_RE = re.compile(r"https?://\S+")
LOGIN_URL_WAIT_SECONDS = 20
LOGIN_URL_POLL_SECONDS = 1


def _find_url(pane_text: str) -> str | None:
    collapsed = re.sub(r"\s+", "", pane_text)
    match = _URL_RE.search(collapsed)
    return match.group(0).rstrip(".,)") if match else None


def _paste_and_enter(text: str):
    """Same load-buffer/paste-buffer approach as session_ask.py — pastes as a
    single bracketed-paste event so long text (or a login code with special
    characters) can't get mis-split the way multiple send-keys calls did."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(text)
        buf_path = f.name
    try:
        _tmux("load-buffer", "-b", "coach-login", buf_path)
        _tmux("paste-buffer", "-b", "coach-login", "-t", SESSION)
    finally:
        os.remove(buf_path)
    _tmux("send-keys", "-t", SESSION, "Enter")


def start_login(provider_name: str) -> dict:
    """Nudges the already-running (login-needed) session into showing its auth
    URL, and returns it. Assumes start_session() already put the pane into the
    login-needed state — this just confirms/advances past any provider-specific
    selection screen and waits for the URL to appear in the pane text."""
    if provider_name in ("claude", "antigravity"):
        # Both show an account/login-method selection menu (from
        # start_session's login_screen_marker match) with the first option
        # (Claude: subscription account / Antigravity: Google OAuth) already
        # highlighted — Enter picks it and reveals the browser URL. Empirically
        # verified for both in this project's development session.
        _tmux("send-keys", "-t", SESSION, "Enter")

    deadline = time.time() + LOGIN_URL_WAIT_SECONDS
    while time.time() < deadline:
        url = _find_url(_pane_text())
        if url:
            return {"url": url}
        time.sleep(LOGIN_URL_POLL_SECONDS)
    return {"url": None}


def submit_login_code(code: str):
    _paste_and_enter(code)


def is_logged_in(provider_name: str) -> bool:
    auth_dir = os.path.expanduser(get_provider(provider_name)["auth_dir"])
    if provider_name == "claude":
        # ~/.claude.json (not ~/.claude/) holds the account record — headless
        # mode alone doesn't populate it, only a completed interactive login does
        # (confirmed by direct testing earlier in this project's history).
        claude_json = os.path.expanduser("~/.claude.json")
        if not os.path.exists(claude_json):
            return False
        import json

        with open(claude_json) as f:
            data = json.load(f)
        return bool(data.get("oauthAccount"))
    if provider_name == "antigravity":
        return os.path.exists(os.path.join(auth_dir, "antigravity-oauth-token"))
    return False


def logout(provider_name: str):
    """Removes the cached credential and restarts the session so the next
    start_session() call shows the login screen again — used when the user
    wants to switch Google/Claude accounts rather than just switching provider."""
    if provider_name == "claude":
        claude_json = os.path.expanduser("~/.claude.json")
        if os.path.exists(claude_json):
            import json

            with open(claude_json) as f:
                data = json.load(f)
            data.pop("oauthAccount", None)
            with open(claude_json, "w") as f:
                json.dump(data, f)
    elif provider_name == "antigravity":
        token_file = os.path.join(os.path.expanduser(get_provider(provider_name)["auth_dir"]), "antigravity-oauth-token")
        if os.path.exists(token_file):
            os.remove(token_file)
    start_session(provider_name)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "start":
        print("Usage: session_manager.py start <provider>", file=sys.stderr)
        sys.exit(1)
    result = start_session(sys.argv[2])
    print(result)
    sys.exit(0 if result["ready"] or result["needs_login"] else 1)
