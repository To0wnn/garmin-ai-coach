#!/bin/sh
set -eu

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "ERROR: CLAUDE_CODE_OAUTH_TOKEN not set" >&2
    exit 1
fi

export CLAUDE_CODE_OAUTH_TOKEN
export TMUX_TMPDIR=/tmp/tmux-shared
mkdir -p "$TMUX_TMPDIR"
chmod 1777 "$TMUX_TMPDIR"

# The home-directory volume may be owned by root if it was just (empty) mounted —
# fix ownership so the 'coach' user can write to it.
chown coach:coach /home/coach

# Permanent tmux session with an always-running claude instance, separate from
# dev-machine sessions — this keeps the system prompt/tool cache warm between
# daily cron calls, avoiding a fresh container start per run (which burned a
# disproportionate amount of the session quota).
# --dangerously-skip-permissions refuses to run as root, hence the 'coach' user.
#
# First a headless warmup call: this writes the account/auth record to
# ~/.claude.json so the interactive session afterwards doesn't ask for a
# browser login again (headless mode and interactive mode turn out to have a
# different auth-verification path — interactive checks for an existing
# account record, headless works directly off the env var).
gosu coach env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" HOME=/home/coach \
    claude -p "ready" --output-format json > /dev/null 2>&1 || true

gosu coach env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" TMUX_TMPDIR="$TMUX_TMPDIR" HOME=/home/coach \
    tmux new-session -d -s coach -x 220 -y 50 "claude --dangerously-skip-permissions"

# Wait until claude has actually finished starting before cron can fire at it.
# Matches both the normal input prompt and (as a fallback) a leftover onboarding prompt.
for i in $(seq 1 30); do
    pane=$(gosu coach env TMUX_TMPDIR="$TMUX_TMPDIR" tmux capture-pane -t coach -p 2>/dev/null || echo "")
    if echo "$pane" | grep -q '│ >'; then
        break
    fi
    if echo "$pane" | grep -qi "select login method"; then
        echo "WARNING: claude is still asking for interactive login despite the warmup call." >&2
        echo "Log in manually once via: docker exec -u coach -e TMUX_TMPDIR=$TMUX_TMPDIR <container> tmux attach -t coach" >&2
        break
    fi
    sleep 1
done

# cron runs with an empty environment — write the required env vars to a file
# that cron_daily.sh reads itself, instead of relying on env vars cron doesn't pass through.
cat > /app/.env.runtime <<EOF
export TMUX_TMPDIR="$TMUX_TMPDIR"
export INFLUXDB_URL="${INFLUXDB_URL:-}"
export INFLUXDB_DB="${INFLUXDB_DB:-GarminStats}"
export LANGUAGE="${LANGUAGE:-English}"
export LOCAL_TZ="${LOCAL_TZ:-Europe/Amsterdam}"
export DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"
EOF
chmod 600 /app/.env.runtime

# Install the cron schedule and run the daemon in the foreground so the
# container stays alive (this IS the container's main process). cron_daily.sh
# itself uses gosu to reach the tmux session as 'coach'.
echo "0 6 * * * root /app/cron_daily.sh >> /var/log/coach-cron.log 2>&1" > /etc/cron.d/coach
chmod 0644 /etc/cron.d/coach
crontab /etc/cron.d/coach
touch /var/log/coach-cron.log
cron -f
