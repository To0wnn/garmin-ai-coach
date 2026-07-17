#!/usr/bin/env python3
"""Stage 5 PREPARATION — NOT wired into cron/dashboard.py yet. This is the
SQLite-backed replacement for coach.py, staged and ready so the actual Stage 5
cutover (after Stage 6's ~2-week soak period proves the new pipeline reliable)
is a file-swap, not a scramble.

Deliberately does NOT duplicate coach.py's prompt text, Discord embed logic, or
locking/logging mechanics — those stay correct and unchanged regardless of the
data source, so this module imports them from coach.py itself (TIP_STRUCTURE,
JSON_SCHEMA_*, build_prompt's static text via the same function, COLOR_MAP,
build_embed, post_discord, _footer_text, read_coach_log/write_coach_log,
LOCK_FILE, LOG_FILE handling). Only the query layer is replaced, sourced from
build_metrics_sqlite.py (Stage 3's already-verified parallel implementation).

At Stage 5 cutover time: this file becomes coach.py (or coach.py's InfluxDB
functions are deleted and build_metrics_sqlite.py's logic is inlined) — see the
migration plan's Stage 5 section for the exact swap steps and go/no-go checks.
"""

import fcntl
import json
import sys
import time
from datetime import timedelta

import build_metrics_sqlite
import coach  # reuses prompt text, Discord/embed logic, locking — see module docstring
import db
import garmin_client
import garmin_sync

# Re-exported from coach.py so this module's own functions (below) read the
# same dashboard-editable settings coach.py already loaded at import time —
# avoids reading settings.py twice or risking the two modules disagreeing.
LOCAL_TZ = coach.LOCAL_TZ
NOW_LOCAL = coach.NOW_LOCAL
IS_WEEKLY = coach.IS_WEEKLY
LOG_HISTORY_DAYS = coach.LOG_HISTORY_DAYS
OUTPUT_FILE = coach.OUTPUT_FILE
LOCK_FILE = coach.LOCK_FILE


def build_metrics() -> dict:
    """SQLite-backed replacement for coach.py's build_metrics() — same output
    shape (verified field-by-field in Stage 3's parity check), sourced from
    db.py instead of InfluxDB. Delegates entirely to build_metrics_sqlite.py
    rather than reimplementing, so there is exactly one SQLite query
    implementation to maintain, not two."""
    return build_metrics_sqlite.build_metrics()


def _activities_since_sqlite(start_date: str) -> list[dict]:
    """SQLite-backed replacement for coach.py's _activities_since(), needed
    here for _backfill_adherence_sqlite() below (coach.py's own version takes
    a datetime and queries InfluxDB — this takes an ISO date string and
    queries SQLite, matching build_metrics_sqlite.py's convention)."""
    return build_metrics_sqlite._activities_since(start_date)


def _backfill_adherence_sqlite(entries: list[dict]) -> list[dict]:
    """Same logic as coach.py's _backfill_adherence(), but sourcing yesterday's
    actual activities from SQLite instead of InfluxDB. coach.py's own
    _compute_adherence/_compute_sport_adherence are reused as-is (pure
    functions, no data-source dependency)."""
    yesterday = NOW_LOCAL.date() - timedelta(days=1)
    for entry in entries:
        if entry.get("weekly") or "adherence" in entry:
            continue
        if entry.get("date") != yesterday.isoformat():
            continue
        actual = [a for a in _activities_since_sqlite(yesterday.isoformat()) if a.get("date") == yesterday.isoformat()]
        entry["adherence"] = coach._compute_adherence(entry, actual)
    return entries


def read_coach_log() -> list[dict]:
    """Identical to coach.py's read_coach_log() — coach_log.json is not part
    of the InfluxDB/SQLite migration at all (it's the coach's own advice
    history, always been a plain JSON file on the coach-home volume), so this
    just delegates directly."""
    return coach.read_coach_log()


def write_coach_log(advice: dict, weekly: bool):
    """Same as coach.py's write_coach_log(), but backfills adherence using
    the SQLite-sourced activity lookup instead of coach.py's InfluxDB one."""
    import os

    entries = _backfill_adherence_sqlite(read_coach_log())
    entries.append(
        {
            "date": NOW_LOCAL.date().isoformat(),
            "weekly": weekly,
            "advice": advice,
        }
    )
    with open(coach.LOG_FILE, "w") as f:
        json.dump(entries, f, ensure_ascii=False)


def build_prompt(metrics: dict, weekly: bool, coach_log: list[dict]) -> str:
    """Delegates to coach.py's build_prompt() unchanged — the prompt text
    itself (TIP_STRUCTURE, field explanations, training philosophy) has
    nothing to do with the data source and must not drift between the two
    implementations. Only the `metrics`/`coach_log` arguments differ (SQLite-
    sourced here vs. InfluxDB-sourced in coach.py's own callers)."""
    return coach.build_prompt(metrics, weekly, coach_log)


def build_chat_context() -> str:
    """SQLite-backed replacement for coach.py's build_chat_context() — used
    by dashboard.py's chat feature once Stage 5 cuts over."""
    metrics = json.dumps(build_metrics(), indent=2, ensure_ascii=False)
    log = json.dumps(read_coach_log(), indent=2, ensure_ascii=False)
    return f"""Here is the user's current training data (for your reference — refer to
concrete numbers from it when relevant, same as your daily advice):

{metrics}

Your own recent advice history:

{log}"""


def call_claude(prompt: str) -> dict:
    """Identical to coach.py's call_claude() — the tmux/session_ask mechanism
    has nothing to do with the data source."""
    import session_ask

    session_ask.ask_and_wait_for_file(prompt, OUTPUT_FILE)
    with open(OUTPUT_FILE) as f:
        response_text = f.read().strip()
    import os

    os.remove(OUTPUT_FILE)

    if response_text.startswith("```"):
        response_text = response_text.strip("`")
        if response_text.startswith("json"):
            response_text = response_text[4:]
        response_text = response_text.strip()
    return json.loads(response_text)


def wait_for_fresh_sync():
    """Replaces coach.py's wait_for_fresh_sync() (which slept, hoping
    garmin-grafana's external 5-min poll had caught up) with a direct pull:
    check db.py's own sync_state.last_sync_at (set by dashboard.py's
    _sync_loop thread); if stale beyond a threshold, synchronously sync
    today's data ourselves before reading, rather than sleeping and hoping an
    external process did it. Strictly more reliable — we control the sync
    now, no need to guess at someone else's cadence."""
    STALE_THRESHOLD_SECONDS = 600  # 10 min — matches the ~5-min sync interval plus headroom
    last_sync_iso = db.get_sync_state("last_sync_at")
    if last_sync_iso:
        from datetime import datetime

        last_sync = datetime.fromisoformat(last_sync_iso)
        age_seconds = (NOW_LOCAL - last_sync).total_seconds()
        if age_seconds < STALE_THRESHOLD_SECONDS:
            return  # recent enough, no need to sync again before reading

    try:
        client = garmin_client.get_client()
    except garmin_client.NotLoggedInError:
        print("Not logged in to Garmin — skipping pre-run sync, reading whatever data exists.")
        return

    today = NOW_LOCAL.date().isoformat()
    print(f"Pulling a fresh sync for {today} before building metrics (last sync was stale or missing).")
    garmin_sync.sync_day(client, today, intraday=True)
    from datetime import datetime as _datetime

    db.set_sync_state("last_sync_at", _datetime.now(LOCAL_TZ).isoformat())


def main():
    wait_for_fresh_sync()
    metrics = build_metrics()
    coach_log = read_coach_log()
    prompt = build_prompt(metrics, IS_WEEKLY, coach_log)
    advice = call_claude(prompt)
    embed = coach.build_embed(advice, IS_WEEKLY)
    coach.post_discord(embed)
    write_coach_log(advice, IS_WEEKLY)
    print(f"Done ({'weekly' if IS_WEEKLY else 'daily'}):", json.dumps(advice, ensure_ascii=False))


if __name__ == "__main__":
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another coach.py/coach_sqlite.py run is already in progress — skipping.", file=sys.stderr)
        sys.exit(1)
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        coach.post_error_to_discord(e)
        sys.exit(1)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
