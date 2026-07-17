#!/bin/sh
set -eu

export TMUX_TMPDIR=/tmp/tmux-shared
mkdir -p "$TMUX_TMPDIR"
chmod 1777 "$TMUX_TMPDIR"

# The home-directory volume may be owned by root if it was just (empty) mounted —
# fix ownership so the 'coach' user can write to it.
chown coach:coach /home/coach

# One-time multi-user migration: adds user_id to every data table (tagging
# existing rows user_id=1), creates the admin account from INITIAL_ADMIN_
# USERNAME/PASSWORD if set, migrates coach_settings.json into the settings
# table, and moves ~/.claude.json/~/.claude//~/.gemini/ into the per-owner
# home session_manager.py now expects. Idempotent — safe to run on every boot.
gosu coach env HOME=/home/coach python3 /app/migrate_to_multiuser.py || \
    echo "WARNING: multi-user migration script failed — check logs above. The dashboard/cron may not work correctly until this is resolved." >&2

# Claude Code's env-var token path stays supported as an alternative to the
# dashboard login flow (see providers.py/session_manager.py) — if set, do the
# same headless warmup call as before so the account record exists before the
# interactive tmux session starts. Gemini CLI has no equivalent split (its
# cached credential file works for both headless and interactive), so this
# step is Claude-specific and skipped entirely for Gemini. Only applies to
# owner_id=1 (the pre-existing single user) since CLAUDE_CODE_OAUTH_TOKEN is
# a container-level env var, not a per-user credential.
PROVIDER_1=$(gosu coach env HOME=/home/coach python3 -c "import settings; print(settings.read_settings(1)['provider'])" 2>/dev/null || echo claude)
if [ "$PROVIDER_1" = "claude" ] && [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    gosu coach env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" HOME=/home/coach/owners/1 \
        claude -p "ready" --output-format json > /dev/null 2>&1 || true
fi

# Permanent tmux session with an always-running CLI, one per distinct AI-
# session owner (not one per user — a user borrowing another's session via a
# share code shouldn't get their own idle session too). session_manager.py
# owns the launch command per provider and the ready/login-needed detection —
# the dashboard calls the exact same function when a user switches providers,
# so there's one implementation instead of a bash version here and a Python
# version there. Uses a small Python one-liner to enumerate owners rather
# than a second dispatch script, since this only runs once at boot.
gosu coach env HOME=/home/coach python3 -c "
import auth
for owner_id in auth.list_distinct_session_owners():
    print(owner_id)
" | while IFS= read -r OWNER_ID; do
    OWNER_PROVIDER=$(gosu coach env HOME=/home/coach python3 -c "import settings; print(settings.read_settings($OWNER_ID)['provider'])")
    gosu coach env HOME=/home/coach CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}" \
        GEMINI_API_KEY="${GEMINI_API_KEY:-}" TMUX_TMPDIR="$TMUX_TMPDIR" \
        python3 /app/session_manager.py start "$OWNER_ID" "$OWNER_PROVIDER" || \
        echo "WARNING: session for owner_id=$OWNER_ID did not reach ready/login state within the startup timeout — check via the dashboard's Settings page." >&2
done

# cron runs with an empty environment — write the required env vars to a file
# that cron_daily.sh reads itself, instead of relying on env vars cron doesn't
# pass through. Just TMUX_TMPDIR now — INFLUXDB_URL/DISCORD_WEBHOOK_URL used
# to live here too, but both are per-user settings now (read from the DB by
# cron_dispatch.py/coach_sqlite.py), not container-level env vars.
cat > /app/.env.runtime <<EOF
export TMUX_TMPDIR="$TMUX_TMPDIR"
EOF
chmod 600 /app/.env.runtime

# Dashboard, background process — best-effort: if it dies the container keeps
# running since cron/coaching (the higher-priority function) is unaffected.
# No process supervision added for one background process.
gosu coach env HOME=/home/coach TMUX_TMPDIR="$TMUX_TMPDIR" \
    DASHBOARD_PORT="${DASHBOARD_PORT:-8420}" \
    python3 /app/dashboard.py &

# Install the cron schedule and run the daemon in the foreground so the
# container stays alive (this IS the container's main process).
# cron_dispatch.py (run every 5 minutes, not once a day at a fixed hour) is
# what makes per-user daily_time changes and new registrations take effect
# without ever needing to rewrite this crontab line again — see cron_
# dispatch.py's own docstring for why a tight-interval dispatcher replaces
# the old single fixed-time entry.
echo "*/5 * * * * root /app/cron_daily.sh >> /var/log/coach-cron.log 2>&1" > /etc/cron.d/coach
chmod 0644 /etc/cron.d/coach
touch /var/log/coach-cron.log
cron -f
