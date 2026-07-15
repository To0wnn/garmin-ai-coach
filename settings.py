#!/usr/bin/env python3
"""Dashboard-editable settings (provider, language, watch device, timezone,
Discord webhook), persisted on the coach-home volume so they survive
rebuilds — same volume coach_log.json already lives on. InfluxDB connection
stays in .env: it's read once at container startup by both coach.py and
dashboard.py before any settings-dependent code runs, and isn't meant to
change without a restart anyway."""

import json
import os

SETTINGS_FILE = os.path.expanduser("~/coach_settings.json")

DEFAULTS = {
    "provider": "claude",
    "language": "English",
    "watch_device": "fenix 8 - 47mm, AMOLED",
    "local_tz": "Europe/Amsterdam",
    "discord_webhook_url": "",
}


# Maps a setting key to the .env var it falls back to when that key is
# missing from the saved settings file — either because the file doesn't
# exist yet (fresh install) or because it predates that key being added
# (e.g. discord_webhook_url didn't exist in coach_settings.json before this
# setting was added to an already-running install).
_ENV_FALLBACKS = {
    "language": "LANGUAGE",
    "watch_device": "WATCH_DEVICE",
    "local_tz": "LOCAL_TZ",
    "discord_webhook_url": "DISCORD_WEBHOOK_URL",
}


def read_settings() -> dict:
    saved = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)

    result = {**DEFAULTS, **saved}
    for key, env_var in _ENV_FALLBACKS.items():
        if key not in saved and env_var in os.environ:
            result[key] = os.environ[env_var]
    return result


def write_settings(updates: dict) -> dict:
    current = read_settings()
    current.update({k: v for k, v in updates.items() if k in DEFAULTS})
    with open(SETTINGS_FILE, "w") as f:
        json.dump(current, f, ensure_ascii=False)
    return current
