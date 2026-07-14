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

# Home-directory-volume kan van root zijn als hij net (leeg) gemount is —
# corrigeer eigendom zodat de 'coach'-gebruiker erin kan schrijven.
chown coach:coach /home/coach

# Permanente tmux-sessie met een altijd-draaiende claude-instance, apart van
# dev-machine sessies (homelab_c) — zo blijft de systeemprompt/tool-cache warm
# tussen de dagelijkse cron-aanroepen in, zonder een verse container-opstart
# per run (dat kostte disproportioneel veel van het sessiequotum).
# --dangerously-skip-permissions weigert als root, dus als 'coach'-gebruiker.
#
# Eerst een headless warmup-call: die schrijft het account/auth-record naar
# ~/.claude.json weg zodat de interactieve sessie daarna niet opnieuw om een
# browser-login vraagt (headless-mode en interactieve mode blijken een
# verschillend auth-verificatiepad te hebben — interactief checkt op een
# bestaand account-record, headless werkt direct op de env-var).
gosu coach env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" HOME=/home/coach \
    claude -p "ready" --output-format json > /dev/null 2>&1 || true

gosu coach env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" TMUX_TMPDIR="$TMUX_TMPDIR" HOME=/home/coach \
    tmux new-session -d -s coach -x 220 -y 50 "claude --dangerously-skip-permissions"

# Wachten tot claude daadwerkelijk klaar is met opstarten voordat cron erop kan schieten.
# Matcht zowel de normale input-prompt als (als vangnet) een resterende onboarding-vraag.
for i in $(seq 1 30); do
    pane=$(gosu coach env TMUX_TMPDIR="$TMUX_TMPDIR" tmux capture-pane -t coach -p 2>/dev/null || echo "")
    if echo "$pane" | grep -q '│ >'; then
        break
    fi
    if echo "$pane" | grep -qi "select login method"; then
        echo "WAARSCHUWING: claude vraagt nog om interactieve login ondanks de warmup-call." >&2
        echo "Log 1x handmatig in via: docker exec -u coach -e TMUX_TMPDIR=$TMUX_TMPDIR <container> tmux attach -t coach" >&2
        break
    fi
    sleep 1
done

# Cron draait met een lege omgeving — schrijf de benodigde env-vars naar een
# bestand dat cron_daily.sh zelf inleest, in plaats van te vertrouwen op env-vars
# die cron niet doorgeeft.
cat > /app/.env.runtime <<EOF
export TMUX_TMPDIR="$TMUX_TMPDIR"
export INFLUXDB_URL="${INFLUXDB_URL:-}"
export INFLUXDB_DB="${INFLUXDB_DB:-GarminStats}"
export LOCAL_TZ="${LOCAL_TZ:-Europe/Amsterdam}"
export DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"
EOF
chmod 600 /app/.env.runtime

# Cron-schema installeren en de daemon op de voorgrond draaien zodat de
# container blijft leven (dit IS het hoofdproces van de container). cron_daily.sh
# gebruikt zelf gosu om als 'coach' bij de tmux-sessie te kunnen.
echo "0 6 * * * root /app/cron_daily.sh >> /var/log/coach-cron.log 2>&1" > /etc/cron.d/coach
chmod 0644 /etc/cron.d/coach
crontab /etc/cron.d/coach
touch /var/log/coach-cron.log
cron -f
