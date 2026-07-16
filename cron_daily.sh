#!/bin/sh
set -eu
cd /app
. /app/.env.runtime
# cron runs this as root with a minimal PATH that doesn't include /usr/sbin —
# gosu (unqualified) silently isn't found there even though it's installed and
# works fine when tested manually via `docker exec` (which inherits a fuller
# shell PATH). Use the absolute path so the daily 06:00 run doesn't depend on
# cron's PATH containing it.
/usr/sbin/gosu coach env TMUX_TMPDIR="$TMUX_TMPDIR" \
    INFLUXDB_URL="$INFLUXDB_URL" INFLUXDB_DB="$INFLUXDB_DB" \
    DISCORD_WEBHOOK_URL="$DISCORD_WEBHOOK_URL" \
    python3 coach.py
