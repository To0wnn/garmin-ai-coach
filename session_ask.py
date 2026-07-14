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
import time

SESSION = "coach"
MAX_WAIT_SECONDS = 180
POLL_INTERVAL = 2
FILE_STABLE_POLLS = 2  # number of identical file sizes in a row before assuming "done"


def _tmux(*args: str) -> str:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True, check=True)
    return result.stdout


def _send_literal_and_enter(text: str):
    _tmux("send-keys", "-t", SESSION, "-l", text)
    _tmux("send-keys", "-t", SESSION, "Enter")


def _clear_session():
    """Clears the conversation so the next cron run starts with a clean
    slate — prevents unbounded context buildup in this long-lived session.
    /clear opens an autocomplete menu; the extra Enter afterwards confirms it."""
    _send_literal_and_enter("/clear")
    time.sleep(2)
    _tmux("send-keys", "-t", SESSION, "Enter")


def ask_and_wait_for_file(prompt: str, output_file: str):
    if os.path.exists(output_file):
        os.remove(output_file)

    _send_literal_and_enter(prompt)

    deadline = time.time() + MAX_WAIT_SECONDS
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
                _clear_session()
                return
        else:
            stable_count = 0
        last_size = size

    _clear_session()
    raise TimeoutError(f"No (stable) answer file within {MAX_WAIT_SECONDS}s: {output_file}")


if __name__ == "__main__":
    import sys

    ask_and_wait_for_file(sys.stdin.read(), sys.argv[1])
    print("done")
