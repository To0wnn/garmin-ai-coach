#!/bin/sh
set -eu
cd /app
. /app/.env.runtime
gosu coach env TMUX_TMPDIR="$TMUX_TMPDIR" \
    INFLUXDB_URL="$INFLUXDB_URL" INFLUXDB_DB="$INFLUXDB_DB" LOCAL_TZ="$LOCAL_TZ" \
    WATCH_DEVICE="$WATCH_DEVICE" \
    LANGUAGE="$LANGUAGE" DISCORD_WEBHOOK_URL="$DISCORD_WEBHOOK_URL" \
    python3 coach.py
