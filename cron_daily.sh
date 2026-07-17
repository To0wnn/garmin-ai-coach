#!/bin/sh
set -eu
# /etc/cron.d/coach has no PATH= line, so Debian cron falls back to its own
# minimal default rather than /etc/crontab's PATH — export a full one here
# instead of relying on a single absolute path staying reachable.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd /app
. /app/.env.runtime
# Cutover: coach_sqlite.py is now the active daily/weekly advice generator
# (SQLite-backed). coach.py/INFLUXDB_URL are still exported here so coach.py's
# InfluxDB path keeps working as a fallback net during the Stage 6 soak
# period — coach_sqlite.py itself doesn't need INFLUXDB_URL, but leaving it
# set is harmless and avoids a second edit if we revert.
gosu coach env TMUX_TMPDIR="$TMUX_TMPDIR" \
    INFLUXDB_URL="$INFLUXDB_URL" INFLUXDB_DB="$INFLUXDB_DB" \
    DISCORD_WEBHOOK_URL="$DISCORD_WEBHOOK_URL" \
    python3 coach_sqlite.py
