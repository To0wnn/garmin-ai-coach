# Garmin AI Coach

Dagelijks (en op zondag uitgebreid) trainingsadvies voor hardlopen én fietsen,
gebaseerd op je Garmin-data uit [garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana),
gegenereerd door Claude en gepost in Discord.

Draait op je eigen Claude-abonnement (Pro/Max) — geen losse API-kosten.

## Vereisten

- Een draaiende [garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana) stack (InfluxDB gevuld met je Garmin-data)
- Een Claude Pro/Max/Team-abonnement
- Een Discord-webhook-URL

## Installatie

```bash
git clone <repo-url> garmin-ai-coach
cd garmin-ai-coach
cp .env.example .env
```

Vul `.env` in:

- `DISCORD_WEBHOOK_URL` — Discord → Serverinstellingen → Integraties → Webhooks → Nieuwe webhook → kies het kanaal → kopieer de URL.
- `CLAUDE_CODE_OAUTH_TOKEN` — genereer met:

```bash
docker compose run --rm --entrypoint claude garmin-ai-coach setup-token
```

Plak het token in `.env` als `CLAUDE_CODE_OAUTH_TOKEN`, dan:

```bash
docker compose up -d
```

**Eenmalig inloggen** (nodig omdat Claude Code's interactieve modus dit los van
het token vraagt):

```bash
docker exec -it -u coach -e TMUX_TMPDIR=/tmp/tmux-shared garmin-ai-coach tmux attach -t coach
```

Volg de login-link in je browser, plak de code, klaar. Detach met `Ctrl+B` `D` —
de sessie blijft draaien. Vanaf nu draait alles zelfstandig, elke ochtend om
06:00 UTC (in te stellen via `LOCAL_TZ`).

## Wat je krijgt

- **Dagelijks**: korte status + concreet advies per sport (type training, duur, doel-hartslag/pace)
- **Zondag**: uitgebreid weekoverzicht met trend-vergelijking (deze week vs. vorige week vs. 4-weken-gemiddelde)
- Houdt rekening met slaap, trainingsbelasting (ACWR), en of je die dag al getraind hebt

## Hoe het werkt

Een permanente Claude Code-sessie draait in de container (via tmux) — cron
stuurt er dagelijks een prompt naartoe met je vooraf-berekende Garmin-cijfers.
Claude schrijft het advies naar een bestand, dat wordt naar Discord gepost.
Na elke run wordt de sessie gereset (`/clear`) zodat context niet opstapelt.
