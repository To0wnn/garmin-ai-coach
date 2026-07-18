#!/usr/bin/env python3
"""Per-user daily/weekly advice dispatcher, replacing cron_daily.sh's single
hardcoded coach_sqlite.py invocation. Run frequently (every 5 minutes, see
entrypoint.sh's crontab line) rather than once a day at a fixed time, because
entrypoint.sh's old approach (write one /etc/cron.d/coach line at container
boot) has no way to react to a new user registering or an existing user
changing their daily_time without a container restart — a tight-interval
dispatcher needs no crontab changes ever again after initial install.

For each user, fires coach_sqlite.py (one subprocess per due user, not an
in-process loop) once their configured local daily_time arrives, guarded
against double-firing within the same calendar day via a per-user
sync_state["last_daily_run_date"]. One subprocess per user — not a shared
in-process call — because coach_sqlite.py's IS_WEEKLY/NOW_LOCAL are read at
MODULE IMPORT time from that user's own settings (see coach.py's module-level
constants); a fresh subprocess per user is what makes each one correctly see
its own timezone/weekly-cutover, without a larger CoachContext rewrite."""

import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import auth
import db
import settings

DISPATCH_WINDOW_MINUTES = 5  # matches the crontab interval this script is run at
COACH_SCRIPT = "/app/coach_sqlite.py"


def _due_now(user_id: int) -> bool:
    s = settings.read_settings(user_id)
    try:
        tz = ZoneInfo(s["local_tz"])
        hh, mm = (int(x) for x in s["daily_time"].split(":"))
    except (ValueError, KeyError):
        print(f"user_id={user_id}: invalid local_tz/daily_time in settings, skipping", file=sys.stderr)
        return False

    now = datetime.now(tz)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = now.date().isoformat()

    already_ran = db.get_sync_state(user_id, "last_daily_run_date") == today
    if already_ran:
        return False

    # Due if "now" has just passed the target time, within one dispatch
    # window — not "any time after target today", which would fire hours
    # late (and immediately) if the dispatcher itself was down around the
    # scheduled time.
    return target <= now < target + timedelta(minutes=DISPATCH_WINDOW_MINUTES)


def _run_user(user_id: int):
    # Marked BEFORE dispatching, not after: coach_sqlite.py waits on a live
    # AI session reply and can easily outlast this dispatcher's 5-minute
    # window, so marking only on success let the next tick re-fire the same
    # user while the first run was still in flight (seen in production: the
    # same date landed 2-3x in coach_log.json). Marking up front trades that
    # for "a run that then fails leaves last_daily_run_date set for today,
    # skipping a same-day retry" — acceptable, since a stuck AI session
    # shouldn't be hammered every 5 minutes anyway.
    today = datetime.now(ZoneInfo(settings.read_settings(user_id)["local_tz"])).date().isoformat()
    db.set_sync_state(user_id, "last_daily_run_date", today)
    print(f"Dispatching daily/weekly advice for user_id={user_id}...")
    result = subprocess.run(["python3", COACH_SCRIPT, str(user_id)], capture_output=True, text=True, timeout=330)
    if result.returncode != 0:
        stderr = result.stderr or result.stdout or "unknown error"
        print(f"user_id={user_id}: run failed: {stderr[-1000:]}", file=sys.stderr)
        return
    print(f"user_id={user_id}: done.")


def run():
    for user in auth.list_users():
        if _due_now(user["id"]):
            _run_user(user["id"])


if __name__ == "__main__":
    run()
