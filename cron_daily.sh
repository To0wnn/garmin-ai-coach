#!/bin/sh
set -eu
# /etc/cron.d/coach has no PATH= line, so Debian cron falls back to its own
# minimal default rather than /etc/crontab's PATH — export a full one here
# instead of relying on a single absolute path staying reachable.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd /app
. /app/.env.runtime
gosu coach env TMUX_TMPDIR="$TMUX_TMPDIR" \
    INFLUXDB_URL="$INFLUXDB_URL" INFLUXDB_DB="$INFLUXDB_DB" \
    DISCORD_WEBHOOK_URL="$DISCORD_WEBHOOK_URL" \
    python3 coach.py
