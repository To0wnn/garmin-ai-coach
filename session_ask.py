#!/usr/bin/env python3
"""Sends a prompt to the permanent 'coach' tmux session (started by
entrypoint.sh) and waits until Claude has written the answer to a file.
Keeps the system prompt/tool cache warm between calls — no new agent-startup
cost per cron run, unlike a fresh `claude -p` subprocess each time.

Parsing terminal screen text (the earlier design) turned out to be fragile:
Claude can do intermediate tool calls, "think" for a while, or wrap the
output across multiple screen lines — any of which broke the earlier
marker/JSON extraction. Having it write a file and polling for that is much
more robust: the file only exists once Claude is done writing."""

import os
import subprocess
import tempfile
import time

SESSION = "coach"
# The daily/weekly prompt got noticeably heavier once more metrics (intensity
# distribution, per-sport load, VO2max, run_target/bike_target) were added —
# a real run took over 80s of "thinking" alone plus tool calls before writing
# the file. 180s cut it close; giving real headroom here.
MAX_WAIT_SECONDS = 300
POLL_INTERVAL = 2
FILE_STABLE_POLLS = 2  # number of identical file sizes in a row before assuming "done"


def _tmux(*args: str) -> str:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True, check=True)
    return result.stdout


# tmux send-keys -l rejects a single argument beyond ~16000 chars ("command too
# long") — the metrics/prompt grew past that limit once run_target/bike_target
# and the other newer metrics were added. Splitting across several send-keys
# calls avoids that limit, but Claude Code's TUI then sees each call as a
# separate paste event ("[Pasted text #10 +301 lines][Pasted text #11 ...]")
# instead of one prompt, and never actually submits it. load-buffer +
# paste-buffer instead loads the whole text as one tmux buffer and pastes it
# as a single bracketed-paste event, however long it is — no per-call size
# limit and no multi-paste fragmentation.
def _send_literal_and_enter(text: str):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(text)
        buf_path = f.name
    try:
        _tmux("load-buffer", "-b", "coach-prompt", buf_path)
        _tmux("paste-buffer", "-b", "coach-prompt", "-t", SESSION)
    finally:
        os.remove(buf_path)
    _tmux("send-keys", "-t", SESSION, "Enter")


PROMPT_FILE = "/app/output/prompt.md"


def _send_via_file_and_enter(text: str):
    """Writes the prompt to a file and sends only a short, single-line instruction
    referencing it, instead of pasting the full prompt into the pane. Confirmed
    necessary for Antigravity CLI: its TUI treats Enter as "new line" rather than
    "submit" once the paste buffer becomes multi-line (which the daily prompt,
    25k+ chars, always is) — a bug in Antigravity/Gemini CLI's own paste handling
    (see google-gemini/gemini-cli issues #15849, #13118; earendil-works/pi#2376),
    not something load-buffer/paste-buffer's length limit can fix, since the paste
    itself succeeds — the CLI just never treats the follow-up Enter as a submit.
    Applied to both providers (not just Antigravity) for one consistent, simpler,
    more robust mechanism rather than a provider-specific special case — Claude
    Code doesn't hit this bug, but sending less raw text through the pane is
    strictly more robust there too as prompts keep growing."""
    with open(PROMPT_FILE, "w") as f:
        f.write(text)
    _send_literal_and_enter(f"Read the file {PROMPT_FILE} and follow the instructions in it.")


def _clear_session():
    """Clears the conversation so the next cron run starts with a clean
    slate — prevents unbounded context buildup in this long-lived session.
    /clear opens an autocomplete menu; the extra Enter afterwards confirms it."""
    _send_literal_and_enter("/clear")
    time.sleep(2)
    _tmux("send-keys", "-t", SESSION, "Enter")


def _wait_for_stable_file(output_file: str, max_wait_seconds: int):
    deadline = time.time() + max_wait_seconds
    last_size = -1
    stable_count = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        if not os.path.exists(output_file):
            continue
        size = os.path.getsize(output_file)
        if size > 0 and size == last_size:
            stable_count += 1
            if stable_count >= FILE_STABLE_POLLS:
                return
        else:
            stable_count = 0
        last_size = size

    # Don't send /clear here — Claude may still be actively generating a
    # response, and /clear would land as queued input mixed in with that,
    # rather than as a clean next turn. Leave the session as-is; the next
    # call's _send_literal_and_enter will queue behind whatever's still
    # running, and a stale leftover file (if one eventually appears) is
    # removed at the top of the next ask-and-wait call.
    raise TimeoutError(f"No (stable) answer file within {max_wait_seconds}s: {output_file}")


def _remove_prompt_file():
    if os.path.exists(PROMPT_FILE):
        os.remove(PROMPT_FILE)


def ask_and_wait_for_file(prompt: str, output_file: str):
    """One-shot: sends prompt, waits for the answer file, then /clear's the
    session so the NEXT call starts with a clean slate. Used by coach.py's
    daily/weekly advice generation, where each run is independent."""
    if os.path.exists(output_file):
        os.remove(output_file)
    _send_via_file_and_enter(prompt)
    _wait_for_stable_file(output_file, MAX_WAIT_SECONDS)
    _remove_prompt_file()
    _clear_session()


def ask_and_wait_for_file_no_clear(prompt: str, output_file: str, max_wait_seconds: int = MAX_WAIT_SECONDS):
    """Same as ask_and_wait_for_file but leaves the session's context intact
    afterward — used by chat_ask.py so a multi-turn conversation can build on
    earlier turns. Caller is responsible for clearing the session explicitly
    when a conversation starts (see chat_ask.start_chat)."""
    if os.path.exists(output_file):
        os.remove(output_file)
    _send_via_file_and_enter(prompt)
    _wait_for_stable_file(output_file, max_wait_seconds)
    _remove_prompt_file()


if __name__ == "__main__":
    import sys

    ask_and_wait_for_file(sys.stdin.read(), sys.argv[1])
    print("done")
