#!/usr/bin/env python3
"""Dashboard-editable per-user settings (provider, language, watch device,
timezone, Discord webhook), stored in coach.db's settings table (user_id-keyed,
reuses the same db.get_setting/set_setting pattern sync_state already uses)
rather than a single flat coach_settings.json — multiple users need their own
independent settings, not one shared file."""

import os

import db

DEFAULTS = {
    "provider": "claude",
    "language": "English",
    "watch_device": "fenix 8 - 47mm, AMOLED",
    "local_tz": "Europe/Amsterdam",
    "discord_webhook_url": "",
    "daily_time": "06:00",  # HH:MM local — when cron_dispatch.py fires this user's daily/weekly advice
}


# Maps a setting key to the .env var it falls back to when that key has never
# been set — ONLY for user_id 1 (the pre-existing single user, migrated from
# the original single-tenant .env-based config by migrate_to_multiuser.py).
# Deliberately NOT applied to any other user_id: these are container-level
# env vars (one Discord webhook, one timezone, etc.), so falling back to them
# for a newly-registered user would leak user 1's real settings (webhook URL
# included) to everyone who signs up — a real bug found and fixed during the
# multi-user rollout's forge verification. New users always get the neutral
# DEFAULTS above until they save their own settings via the dashboard.
_ENV_FALLBACKS = {
    "language": "LANGUAGE",
    "watch_device": "WATCH_DEVICE",
    "local_tz": "LOCAL_TZ",
    "discord_webhook_url": "DISCORD_WEBHOOK_URL",
}
_ENV_FALLBACK_USER_ID = 1


def read_settings(user_id: int) -> dict:
    result = dict(DEFAULTS)
    for key in DEFAULTS:
        saved = db.get_setting(user_id, key)
        if saved is not None:
            result[key] = saved
        elif user_id == _ENV_FALLBACK_USER_ID and key in _ENV_FALLBACKS and _ENV_FALLBACKS[key] in os.environ:
            result[key] = os.environ[_ENV_FALLBACKS[key]]
    return result


def write_settings(user_id: int, updates: dict) -> dict:
    for key, value in updates.items():
        if key in DEFAULTS:
            db.set_setting(user_id, key, value)
    return read_settings(user_id)
