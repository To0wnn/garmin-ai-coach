#!/usr/bin/env python3
"""Defines what differs between AI CLI backends the coach can drive inside the
'coach' tmux session. session_ask.py's send/poll/clear mechanics are the same
for any provider — only the launch command, readiness detection, login
handling, and the tool name used in the prompt differ."""

PROVIDERS = {
    "claude": {
        "label": "Claude Code",
        "start_cmd": "claude --dangerously-skip-permissions",
        # The prompt-line border glyph varies by pane width/rendering (seen both
        # "│ >" and "❯" across sessions) — "bypass permissions on" in the footer
        # is stable regardless of that, and only appears once the session is
        # actually ready for input (not during onboarding/login screens).
        "ready_marker": "bypass permissions on",
        "write_tool_name": "Write",
        "auth_dir": "~/.claude",
        # Claude Code prompts for login automatically on first interactive use
        # when no cached account record exists — no explicit login command needed,
        # just start the session and watch for the login screen.
        "login_screen_marker": "select login method",
    },
    "antigravity": {
        "label": "Antigravity CLI (Gemini)",
        # Gemini CLI's free/individual "Sign in with Google" OAuth path was
        # discontinued by Google (June 2026) — Antigravity CLI is the
        # replacement, same tmux/skip-permissions pattern. Empirically
        # verified end-to-end against the real binary (v1.1.2): logged in via
        # the paste-code OAuth flow, confirmed no gosu/non-root requirement
        # (runs fine as root, unlike Claude), confirmed a real prompt writes
        # a file via its file-write tool, and confirmed /clear resets the
        # conversation — see project memory for the verification session.
        # install.sh puts the binary in ~/.local/bin, which isn't on PATH by
        # default for a non-login shell — prefixed explicitly rather than
        # relying on shell profile sourcing inside a scripted tmux session.
        "start_cmd": "PATH=$HOME/.local/bin:$PATH agy --dangerously-skip-permissions",
        # The ready prompt has no distinctive static label (just a bare "> "
        # input line) — "? for shortcuts" in the footer is the reliable marker
        # once past onboarding.
        "ready_marker": "? for shortcuts",
        "write_tool_name": "the file-write tool",
        # ~/.gemini/antigravity-cli/antigravity-oauth-token — confirmed by
        # inspecting the directory after a real login, not ~/.antigravity as
        # first assumed (Antigravity CLI nests its state under Gemini's
        # config dir since it shares lineage with Gemini CLI).
        "auth_dir": "~/.gemini/antigravity-cli",
        "login_screen_marker": "you are currently not signed in",
    },
}


def get_provider(name: str) -> dict:
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider: {name!r} (known: {list(PROVIDERS)})")
    return PROVIDERS[name]
