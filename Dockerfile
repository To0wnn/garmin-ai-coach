FROM node:lts-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl python3 ca-certificates tmux cron gosu && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# claude --dangerously-skip-permissions weigert te draaien als root — de tmux/claude
# sessie draait daarom als deze aparte gebruiker, cron zelf blijft als root (vereist).
# Antigravity CLI (agy) heeft geen root-restrictie (empirisch geverifieerd), maar
# dezelfde non-root user wordt ook daarvoor gebruikt: simpeler dan providers
# verschillend te behandelen, en install.sh installeert toch al naar $HOME/.local/bin.
RUN useradd -m -s /bin/sh coach

# Antigravity CLI: geen npm-package (Gemini CLI's opvolger na het stopzetten van
# de gratis individuele OAuth-login, juni 2026) — een los Go-binary via installer-script,
# geïnstalleerd als de 'coach'-gebruiker zodat het in diens $HOME/.local/bin terechtkomt.
USER coach
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash
USER root

WORKDIR /app
COPY coach.py /app/coach.py
COPY session_ask.py /app/session_ask.py
COPY session_manager.py /app/session_manager.py
COPY providers.py /app/providers.py
COPY settings.py /app/settings.py
COPY dashboard.py /app/dashboard.py
COPY dashboard.html /app/dashboard.html
COPY entrypoint.sh /app/entrypoint.sh
COPY cron_daily.sh /app/cron_daily.sh
RUN mkdir -p /app/output && \
    chmod +x /app/entrypoint.sh /app/cron_daily.sh && \
    chown -R coach:coach /app

ENTRYPOINT ["/app/entrypoint.sh"]
