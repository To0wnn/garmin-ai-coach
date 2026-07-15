# Garmin AI Coach

Daily (and an in-depth weekly review on Sundays) training advice for running
and cycling, based on your Garmin data from
[garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana), generated
by Claude and posted to Discord.

Runs on your own Claude subscription (Pro/Max) — no separate API costs.

## Requirements

- A running [garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana) stack (InfluxDB filled with your Garmin data)
- A Claude Pro/Max/Team subscription
- A Discord webhook URL

## Installation

```bash
git clone <repo-url> garmin-ai-coach
cd garmin-ai-coach
cp .env.example .env
```

Fill in `.env`:

- `INFLUXDB_URL` / `INFLUXDB_DB` — point these at your garmin-grafana InfluxDB.
- `DISCORD_WEBHOOK_URL` — Discord → Server Settings → Integrations → Webhooks → New Webhook → pick the channel → copy the URL.
- `LANGUAGE` — the language the advice is written in (e.g. `English`, `Nederlands`, `Deutsch`, `Español`). Defaults to English.
- `WATCH_DEVICE` — exact device name as it appears in your InfluxDB `Device` tag (e.g. `fenix 8 - 47mm, AMOLED`). Several fields are only reliable from the watch itself, not from paired sensors (HRM strap, bike computer) — set this if your device name differs from the default.
- `CLAUDE_CODE_OAUTH_TOKEN` — generate with:

```bash
docker compose run --rm --entrypoint claude garmin-ai-coach setup-token
```

Paste the token into `.env`, then:

```bash
docker compose up -d
```

**One-time login** (needed because Claude Code's interactive mode requires
this separately from the token):

```bash
docker exec -it -u coach -e TMUX_TMPDIR=/tmp/tmux-shared garmin-ai-coach tmux attach -t coach
```

Follow the login link in your browser, paste the code, done. Detach with
`Ctrl+B` `D` — the session keeps running. From here on everything runs on its
own, every morning at 06:00 UTC (adjustable via `LOCAL_TZ`).

## What you get

- **Daily**: short status update + concrete advice per sport (workout type, duration, target heart rate/pace)
- **Sunday**: in-depth weekly review with trend comparison (this week vs. last week vs. 4-week average)
- Takes sleep (hours + Garmin's sleep score), training load (ACWR), HRV/resting-HR baseline deviation, recent training history (14 days), and whether you've already trained that day into account
- Remembers its own past advice (persisted log) to stay consistent day to day instead of starting from zero every run
- Evidence-based: every recommendation references a specific number, not vague statements

## How it works

A permanent Claude Code session runs inside the container (via tmux) — cron
sends it a prompt every day with your pre-computed Garmin numbers. Claude
writes the advice to a file, which gets posted to Discord. After every run
the session is reset (`/clear`) so context doesn't build up over time.
