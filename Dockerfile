FROM node:lts-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl python3 ca-certificates tmux cron gosu && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# claude --dangerously-skip-permissions weigert te draaien als root — de tmux/claude
# sessie draait daarom als deze aparte gebruiker, cron zelf blijft als root (vereist).
RUN useradd -m -s /bin/sh coach

WORKDIR /app
COPY coach.py /app/coach.py
COPY session_ask.py /app/session_ask.py
COPY dashboard.py /app/dashboard.py
COPY dashboard.html /app/dashboard.html
COPY entrypoint.sh /app/entrypoint.sh
COPY cron_daily.sh /app/cron_daily.sh
RUN mkdir -p /app/output && \
    chmod +x /app/entrypoint.sh /app/cron_daily.sh && \
    chown -R coach:coach /app

ENTRYPOINT ["/app/entrypoint.sh"]
