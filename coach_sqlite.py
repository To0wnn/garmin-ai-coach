#!/usr/bin/env python3
"""SQLite-backed advice pipeline — the live entry point (cron_daily.sh,
dashboard.py's "Run now" button, and dashboard.py's chat all call into this
module). Deliberately does NOT duplicate coach.py's prompt text, Discord embed
logic, or logging mechanics — those stay correct and shared regardless of
which user/owner they're run for, so this module imports them from coach.py
itself (TIP_STRUCTURE, JSON_SCHEMA_*, build_prompt's static text via the same
function, COLOR_MAP, build_embed, post_discord, _footer_text, read_coach_log).
Only the query layer is this module's own, sourced from build_metrics_sqlite.py.

coach.py no longer has an InfluxDB path or a competing entry point at all
(removed as part of the multi-user work's Stage 5) — this has been the sole
production advice pipeline since the InfluxDB->SQLite cutover."""

import fcntl
import json
import sys
import time
from datetime import timedelta

import build_metrics_sqlite
import coach  # reuses prompt text, Discord/embed logic — see module docstring
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


def build_metrics(user_id: int) -> dict:
    """SQLite-backed replacement for coach.py's build_metrics() — same output
    shape (verified field-by-field in Stage 3's parity check), sourced from
    db.py instead of InfluxDB. Delegates entirely to build_metrics_sqlite.py
    rather than reimplementing, so there is exactly one SQLite query
    implementation to maintain, not two."""
    return build_metrics_sqlite.build_metrics(user_id)


def vo2max_series(user_id: int, days: int) -> dict:
    """SQLite-backed replacement for coach.py's vo2max_series() — used by
    dashboard.py's /api/data for the VO2max trend chart."""
    return build_metrics_sqlite.vo2max_series(user_id, days)


def _activities_since_sqlite(user_id: int, start_date: str) -> list[dict]:
    """SQLite-backed replacement for coach.py's _activities_since(), needed
    here for _backfill_adherence_sqlite() below (coach.py's own version takes
    a datetime and queries InfluxDB — this takes an ISO date string and
    queries SQLite, matching build_metrics_sqlite.py's convention)."""
    return build_metrics_sqlite._activities_since(user_id, start_date)


def _backfill_adherence_sqlite(user_id: int, entries: list[dict]) -> list[dict]:
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
        actual = [a for a in _activities_since_sqlite(user_id, yesterday.isoformat()) if a.get("date") == yesterday.isoformat()]
        entry["adherence"] = coach._compute_adherence(entry, actual)
    return entries


def read_coach_log(user_id: int) -> list[dict]:
    """Identical to coach.py's read_coach_log() — coach_log.json is not part
    of the InfluxDB/SQLite migration at all (it's the coach's own advice
    history, always been a plain JSON file on the coach-home volume), so this
    just delegates directly. Per-user (see coach.owner_log_file) — each
    user's advice history is their own, not shared."""
    return coach.read_coach_log(user_id)


def write_coach_log(user_id: int, advice: dict, weekly: bool):
    """Same as coach.py's write_coach_log(), but backfills adherence using
    the SQLite-sourced activity lookup instead of coach.py's InfluxDB one.
    Writes to this user's own coach_log.json (coach.owner_log_file)."""
    entries = _backfill_adherence_sqlite(user_id, read_coach_log(user_id))
    entries.append(
        {
            "date": NOW_LOCAL.date().isoformat(),
            "weekly": weekly,
            "advice": advice,
        }
    )
    with open(coach.owner_log_file(user_id), "w") as f:
        json.dump(entries, f, ensure_ascii=False)


def build_prompt(metrics: dict, weekly: bool, coach_log: list[dict], owner_id: int) -> str:
    """Delegates to coach.py's build_prompt() unchanged — the prompt text
    itself (TIP_STRUCTURE, field explanations, training philosophy) has
    nothing to do with the data source and must not drift between the two
    implementations. owner_id is needed because the prompt itself instructs
    the AI to write its answer to that owner's output file (see coach.
    owner_output_file / the AI-session sharing model)."""
    return coach.build_prompt(metrics, weekly, coach_log, owner_id)


def build_chat_context(user_id: int) -> str:
    """SQLite-backed replacement for coach.py's build_chat_context() — used
    by dashboard.py's chat feature once Stage 5 cuts over."""
    metrics = json.dumps(build_metrics(user_id), indent=2, ensure_ascii=False)
    log = json.dumps(read_coach_log(user_id), indent=2, ensure_ascii=False)
    return f"""Here is the user's current training data (for your reference — refer to
concrete numbers from it when relevant, same as your daily advice):

{metrics}

Your own recent advice history:

{log}"""


def call_claude(owner_id: int, prompt: str) -> dict:
    """Same as coach.py's call_claude() — the tmux/session_ask mechanism has
    nothing to do with the data source. owner_id is the EFFECTIVE AI-session
    owner (own session, or a borrowed owner's if a share code was redeemed —
    see session_ask.ask_and_wait_for_file's docstring), not necessarily the
    requesting user."""
    import os

    import session_ask

    output_file = coach.owner_output_file(owner_id)
    session_ask.ask_and_wait_for_file(owner_id, prompt, output_file)
    with open(output_file) as f:
        response_text = f.read().strip()
    os.remove(output_file)

    if response_text.startswith("```"):
        response_text = response_text.strip("`")
        if response_text.startswith("json"):
            response_text = response_text[4:]
        response_text = response_text.strip()
    return json.loads(response_text)


def wait_for_fresh_sync(user_id: int):
    """Replaces coach.py's wait_for_fresh_sync() (which slept, hoping
    garmin-grafana's external 5-min poll had caught up) with a direct pull:
    check db.py's own sync_state.last_sync_at (set by dashboard.py's
    _sync_loop thread); if stale beyond a threshold, synchronously sync
    today's data ourselves before reading, rather than sleeping and hoping an
    external process did it. Strictly more reliable — we control the sync
    now, no need to guess at someone else's cadence."""
    STALE_THRESHOLD_SECONDS = 600  # 10 min — matches the ~5-min sync interval plus headroom
    last_sync_iso = db.get_sync_state(user_id, "last_sync_at")
    if last_sync_iso:
        from datetime import datetime

        last_sync = datetime.fromisoformat(last_sync_iso)
        age_seconds = (NOW_LOCAL - last_sync).total_seconds()
        if age_seconds < STALE_THRESHOLD_SECONDS:
            return  # recent enough, no need to sync again before reading

    try:
        client = garmin_client.get_client(user_id)
    except garmin_client.NotLoggedInError:
        print("Not logged in to Garmin — skipping pre-run sync, reading whatever data exists.")
        return

    today = NOW_LOCAL.date().isoformat()
    print(f"Pulling a fresh sync for {today} before building metrics (last sync was stale or missing).")
    garmin_sync.sync_day(user_id, client, today, intraday=True)
    from datetime import datetime as _datetime

    db.set_sync_state(user_id, "last_sync_at", _datetime.now(LOCAL_TZ).isoformat())


def main(user_id: int = 1):
    # user_id defaults to 1 (the pre-existing single user) — real per-user
    # dispatching (multiple users, each with their own schedule) lands in
    # Stage 6/10, not here; Stage 3/4 only thread user_id/owner_id through the
    # layers underneath so they're ready for that dispatcher to call into.
    import auth

    owner_id = auth.session_owner_id_of(auth.get_user_by_id(user_id))
    wait_for_fresh_sync(user_id)
    metrics = build_metrics(user_id)
    coach_log = read_coach_log(user_id)
    prompt = build_prompt(metrics, IS_WEEKLY, coach_log, owner_id)
    advice = call_claude(owner_id, prompt)
    embed = coach.build_embed(advice, IS_WEEKLY)
    coach.post_discord(embed)
    write_coach_log(user_id, advice, IS_WEEKLY)
    print(f"Done ({'weekly' if IS_WEEKLY else 'daily'}):", json.dumps(advice, ensure_ascii=False))


if __name__ == "__main__":
    # Locking is per EFFECTIVE SESSION OWNER, not per user_id — two users
    # sharing one owner's session must still serialize through the same lock
    # file; two users with independent sessions must not block each other.
    # Resolved once here (before main()) since it's also needed for the lock
    # file path itself, not just call_claude() inside main().
    #
    # user_id comes from argv[1] (cron_dispatch.py passes it explicitly, one
    # subprocess per due user) — defaults to 1 so dashboard.py's "Run now"
    # button (which invokes this script with no arguments) keeps working
    # unchanged.
    import auth as _auth

    _user_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    _owner_id = _auth.session_owner_id_of(_auth.get_user_by_id(_user_id))
    lock_fd = open(coach.owner_lock_file(_owner_id), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another coach.py/coach_sqlite.py run is already in progress for this AI session — skipping.", file=sys.stderr)
        sys.exit(1)
    try:
        main(_user_id)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        coach.post_error_to_discord(e)
        sys.exit(1)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
