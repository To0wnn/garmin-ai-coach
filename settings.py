#!/usr/bin/env python3
"""Dashboard-editable settings (provider, language, watch device, timezone),
persisted on the coach-home volume so they survive rebuilds — same volume
coach_log.json already lives on. Discord webhook and InfluxDB connection stay
in .env: they're read once at container startup and aren't meant to change
without a restart anyway, so there's no benefit to moving them here."""

import json
import os

SETTINGS_FILE = os.path.expanduser("~/coach_settings.json")

DEFAULTS = {
    "provider": "claude",
    "language": "English",
    "watch_device": "fenix 8 - 47mm, AMOLED",
    "local_tz": "Europe/Amsterdam",
}


def read_settings() -> dict:
    """Falls back to .env values on first run (upgrading an existing install
    shouldn't silently reset LANGUAGE/WATCH_DEVICE/LOCAL_TZ to the hardcoded
    defaults above), then to DEFAULTS if neither is set."""
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        return {**DEFAULTS, **saved}

    return {
        "provider": "claude",
        "language": os.environ.get("LANGUAGE", DEFAULTS["language"]),
        "watch_device": os.environ.get("WATCH_DEVICE", DEFAULTS["watch_device"]),
        "local_tz": os.environ.get("LOCAL_TZ", DEFAULTS["local_tz"]),
    }


def write_settings(updates: dict) -> dict:
    current = read_settings()
    current.update({k: v for k, v in updates.items() if k in DEFAULTS})
    with open(SETTINGS_FILE, "w") as f:
        json.dump(current, f, ensure_ascii=False)
    return current
