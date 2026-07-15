#!/bin/sh
set -eu

export TMUX_TMPDIR=/tmp/tmux-shared
mkdir -p "$TMUX_TMPDIR"
chmod 1777 "$TMUX_TMPDIR"

# The home-directory volume may be owned by root if it was just (empty) mounted —
# fix ownership so the 'coach' user can write to it.
chown coach:coach /home/coach

PROVIDER=$(gosu coach python3 -c "import settings; print(settings.read_settings()['provider'])")

# Claude Code's env-var token path stays supported as an alternative to the
# dashboard login flow (see providers.py/session_manager.py) — if set, do the
# same headless warmup call as before so the account record exists before the
# interactive tmux session starts. Gemini CLI has no equivalent split (its
# cached credential file works for both headless and interactive), so this
# step is Claude-specific and skipped entirely for Gemini.
if [ "$PROVIDER" = "claude" ] && [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    gosu coach env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" HOME=/home/coach \
        claude -p "ready" --output-format json > /dev/null 2>&1 || true
fi

# Permanent tmux session with an always-running CLI, separate from dev-machine
# sessions — this keeps the system prompt/tool cache warm between daily cron
# calls, avoiding a fresh container start per run (which burned a
# disproportionate amount of the session quota). session_manager.py owns the
# launch command per provider and the ready/login-needed detection — the
# dashboard calls the exact same function when the user switches providers,
# so there's one implementation instead of a bash version here and a Python
# version there.
gosu coach env HOME=/home/coach CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}" \
    GEMINI_API_KEY="${GEMINI_API_KEY:-}" TMUX_TMPDIR="$TMUX_TMPDIR" \
    python3 /app/session_manager.py start "$PROVIDER" || \
    echo "WARNING: session did not reach ready/login state within the startup timeout — check via the dashboard's Settings page." >&2

# cron runs with an empty environment — write the required env vars to a file
# that cron_daily.sh reads itself, instead of relying on env vars cron doesn't pass through.
cat > /app/.env.runtime <<EOF
export TMUX_TMPDIR="$TMUX_TMPDIR"
export INFLUXDB_URL="${INFLUXDB_URL:-}"
export INFLUXDB_DB="${INFLUXDB_DB:-GarminStats}"
export DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"
EOF
chmod 600 /app/.env.runtime

# Dashboard, background process — best-effort: if it dies the container keeps
# running since cron/coaching (the higher-priority function) is unaffected.
# No process supervision added for one background process. Needs the full
# coach.py env (not just InfluxDB vars) because its "run now" button invokes
# coach.py directly as a subprocess, inheriting this process's environment.
gosu coach env HOME=/home/coach TMUX_TMPDIR="$TMUX_TMPDIR" \
    INFLUXDB_URL="${INFLUXDB_URL:-}" INFLUXDB_DB="${INFLUXDB_DB:-GarminStats}" \
    DASHBOARD_PORT="${DASHBOARD_PORT:-8420}" \
    DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}" \
    python3 /app/dashboard.py &

# Install the cron schedule and run the daemon in the foreground so the
# container stays alive (this IS the container's main process). cron_daily.sh
# itself uses gosu to reach the tmux session as 'coach'.
echo "0 6 * * * root /app/cron_daily.sh >> /var/log/coach-cron.log 2>&1" > /etc/cron.d/coach
chmod 0644 /etc/cron.d/coach
touch /var/log/coach-cron.log
cron -f
