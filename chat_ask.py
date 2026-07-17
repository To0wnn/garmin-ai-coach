#!/usr/bin/env python3
"""Multi-turn chat with the permanent 'coach' tmux session, for the dashboard's
ad-hoc Q&A panel — distinct from session_ask.py's one-shot ask_and_wait_for_file
(which always /clear's afterward for the daily/weekly advice flow). A chat
conversation instead clears ONCE at the start (start_chat) and then sends each
turn without clearing, so the session remembers earlier turns."""

import os

import session_ask

# Conversational replies are much shorter than the daily/weekly JSON-advice
# generation (that one observed 80s+ of "thinking" alone) — 60s is generous
# headroom for a plain-text answer to a follow-up question.
CHAT_MAX_WAIT_SECONDS = 60


def chat_output_file(owner_id: int) -> str:
    return f"/app/output/{owner_id}/chat_reply.txt"


def start_chat(owner_id: int):
    """Clears any existing session context — call once when a new conversation
    begins. Reusing session_ask's own clear function rather than duplicating
    the '/clear + confirm Enter' sequence."""
    session_ask._clear_session(owner_id)


def send_chat_message(owner_id: int, prompt: str, write_tool_name: str) -> str:
    """Sends one message in an already-started conversation and returns the
    reply text. Does NOT clear context before or after — that's what makes
    this multi-turn. The reply is written to a file by the CLI itself (same
    robust file-write-and-poll pattern as the daily JSON advice) rather than
    scraped from the tmux screen, which would be even more fragile for
    free-form conversational text than it already proved to be for JSON.
    owner_id is the EFFECTIVE AI-session owner (see session_ask.ask_and_wait_
    for_file's docstring) — the chat happens in that owner's tmux pane."""
    output_file = chat_output_file(owner_id)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    wrapped = (
        f"{prompt}\n\nWrite your reply as plain text (no markdown, no code "
        f"fences) to the file {output_file} using {write_tool_name}. "
        f"Keep it conversational — a few sentences, not a report."
    )
    session_ask.ask_and_wait_for_file_no_clear(owner_id, wrapped, output_file, CHAT_MAX_WAIT_SECONDS)
    with open(output_file) as f:
        reply = f.read().strip()
    os.remove(output_file)
    return reply
