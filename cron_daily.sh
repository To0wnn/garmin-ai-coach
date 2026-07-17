#!/bin/sh
set -eu
# /etc/cron.d/coach has no PATH= line, so Debian cron falls back to its own
# minimal default rather than /etc/crontab's PATH — export a full one here
# instead of relying on a single absolute path staying reachable.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd /app
. /app/.env.runtime
# Runs every 5 minutes (see entrypoint.sh's crontab line) — cron_dispatch.py
# itself decides which users (if any) are actually due right now, based on
# each user's own daily_time/local_tz setting, so most invocations of this
# script do nothing. Replaces the old single hardcoded coach_sqlite.py call
# now that there can be more than one user with more than one schedule.
gosu coach env TMUX_TMPDIR="$TMUX_TMPDIR" python3 cron_dispatch.py
