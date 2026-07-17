# Debian trixie (13) ships Python 3.13 by default — needed for python-garminconnect
# (requires Python >=3.12). node:lts-slim still runs on bookworm (Debian 12, Python
# 3.11), so this switches to a plain Debian trixie base and installs Node itself,
# rather than waiting on an official trixie-based node image to exist.
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl python3 python3-pip ca-certificates gnupg tmux cron gosu && \
    rm -rf /var/lib/apt/lists/*

# python-garminconnect (own Garmin Connect integration, replacing garmin-grafana) —
# trixie's system Python is PEP-668-protected ("externally-managed-environment"),
# so --break-system-packages is required. No venv: this container runs nothing else
# on this Python, and the project's existing convention is stdlib/system-Python
# throughout (no requirements.txt, no venv infrastructure) — consistent with that.
RUN pip3 install --break-system-packages --no-cache-dir garminconnect==0.3.6

# Debian trixie's own nodejs/npm packages are Node 20, which npm flags as below
# @anthropic-ai/claude-code's required engine (>=22) — NodeSource's setup script
# installs a supported Node 22 instead, verified working end-to-end (claude --version,
# no engine warning) against this exact base.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# claude --dangerously-skip-permissions weigert te draaien als root — de tmux/claude
# sessie draait daarom als deze aparte gebruiker, cron zelf blijft als root (vereist).
# Antigravity CLI (agy) heeft geen root-restrictie (empirisch geverifieerd), maar
# dezelfde non-root user wordt ook daarvoor gebruikt: simpeler dan providers
# verschillend te behandelen, en install.sh installeert toch al naar $HOME/.local/bin.
# UID/GID EXPLICIET vastgepind op 1001 — de bookworm-basis (node:lts-slim) gaf coach
# toevallig UID 1001, en het bestaande coach-home-volume (tokens, coach_log,
# settings) is op die UID geschreven. Zonder deze pin geeft useradd op een nieuwe
# basis-image (bijv. deze overstap naar debian:trixie-slim) een andere UID (1000),
# waardoor de coach-user het bestaande volume niet meer kan lezen/schrijven —
# precies dit gebeurde bij de trixie-overstap (PermissionError op ~/.claude.json).
RUN useradd -u 1001 -m -s /bin/sh coach

# Antigravity CLI: geen npm-package (Gemini CLI's opvolger na het stopzetten van
# de gratis individuele OAuth-login, juni 2026) — een los Go-binary via installer-script,
# geïnstalleerd als de 'coach'-gebruiker zodat het in diens $HOME/.local/bin terechtkomt.
USER coach
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash
USER root

WORKDIR /app
COPY coach.py /app/coach.py
COPY coach_sqlite.py /app/coach_sqlite.py
COPY build_metrics_sqlite.py /app/build_metrics_sqlite.py
COPY auth.py /app/auth.py
COPY cron_dispatch.py /app/cron_dispatch.py
COPY migrate_to_multiuser.py /app/migrate_to_multiuser.py
COPY session_ask.py /app/session_ask.py
COPY session_manager.py /app/session_manager.py
COPY providers.py /app/providers.py
COPY settings.py /app/settings.py
COPY chat_ask.py /app/chat_ask.py
COPY db.py /app/db.py
COPY garmin_client.py /app/garmin_client.py
COPY garmin_sync.py /app/garmin_sync.py
COPY dashboard.py /app/dashboard.py
COPY dashboard.html /app/dashboard.html
COPY entrypoint.sh /app/entrypoint.sh
COPY cron_daily.sh /app/cron_daily.sh
RUN mkdir -p /app/output && \
    chmod +x /app/entrypoint.sh /app/cron_daily.sh && \
    chown -R coach:coach /app

ENTRYPOINT ["/app/entrypoint.sh"]
