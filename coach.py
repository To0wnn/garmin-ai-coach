#!/usr/bin/env python3
"""Garmin AI coach: queries InfluxDB, aggregates metrics, asks Claude Code for
advice, posts the result to Discord. Runs daily; on Sundays does a full weekly
review instead of the short daily check-in."""

import fcntl
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import settings as _settings
from providers import get_provider

_SETTINGS = _settings.read_settings()

INFLUXDB_URL = os.environ["INFLUXDB_URL"]  # e.g. http://172.17.0.1:8187
INFLUXDB_DB = os.environ.get("INFLUXDB_DB", "GarminStats")
# Dashboard-editable (settings.py / ~/coach_settings.json) — not read from .env
# directly anymore, so a provider/language/watch-device/webhook change on the
# settings page takes effect on the next run without a container restart.
PROVIDER = _SETTINGS["provider"]
LANGUAGE = _SETTINGS["language"]  # e.g. English, Nederlands, Deutsch
DISCORD_WEBHOOK_URL = _SETTINGS["discord_webhook_url"]
# DailyStats/SleepSummary carry a per-device tag (watch, HRM strap, bike computer,
# speed sensor) — several fields (totalSteps, stressPercentage) are only meaningful
# from the watch itself and silently wrong from other devices (e.g. a chest strap
# reporting 227 steps vs. the watch's 1790 for the same day). Filtering on this
# pins those queries to the one full/reliable source.
WATCH_DEVICE = _SETTINGS["watch_device"]
LOCAL_TZ = ZoneInfo(_SETTINGS["local_tz"])
NOW_LOCAL = datetime.now(LOCAL_TZ)
IS_WEEKLY = NOW_LOCAL.weekday() == 6  # Sunday, in lokale tijd
STALE_AFTER_HOURS = 36
OUTPUT_FILE = "/app/output/advies.json"
# The daily cron run and a manual "run now" dashboard click both ultimately talk
# to the same single tmux/claude session — an overlap sends two prompts before
# either gets an Enter, which Claude Code's TUI shows as two separate, never-
# submitted "[Pasted text #N]" placeholders (a real incident, not hypothetical).
# A cross-process file lock (not dashboard.py's in-process threading.Lock, which
# only protects against overlaps within that one process) closes this regardless
# of which of the two entry points is running.
LOCK_FILE = "/tmp/coach.lock"
# Persisted on the coach-home volume so it survives container restarts/rebuilds.
LOG_FILE = os.path.expanduser("~/coach_log.json")
LOG_HISTORY_DAYS = 14

SPORT_TYPES = {
    "running": ["running"],
    "cycling": ["road_biking", "indoor_cycling", "mountain_biking", "gravel_cycling", "cycling"],
}


def local_midnight_utc(days_ago: int = 0) -> datetime:
    """Midnight in the local timezone, N days ago, converted to UTC."""
    midnight_local = NOW_LOCAL.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return midnight_local.astimezone(timezone.utc)


def influx_query(q: str) -> list[dict]:
    params = urllib.parse.urlencode({"db": INFLUXDB_DB, "q": q})
    with urllib.request.urlopen(f"{INFLUXDB_URL}/query?{params}", timeout=15) as resp:
        data = json.load(resp)
    result = data.get("results", [{}])[0]
    if "error" in result:
        raise RuntimeError(f"InfluxDB query error: {result['error']} (query: {q})")
    series = result.get("series")
    if not series:
        return []
    cols = series[0]["columns"]
    return [dict(zip(cols, row)) for row in series[0]["values"]]


def avg(rows: list[dict], field: str) -> float | None:
    vals = [r[field] for r in rows if r.get(field) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def latest(measurement: str) -> dict:
    """Most recent data point, with a staleness flag so the prompt knows whether
    the data is still fresh enough to rely on."""
    rows = influx_query(f'SELECT * FROM "{measurement}" ORDER BY time DESC LIMIT 1')
    if not rows:
        return {"available": False}
    row = rows[0]
    ts = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    row["_stale"] = age_hours > STALE_AFTER_HOURS
    row["_age_hours"] = round(age_hours, 1)
    return row


def daily_stats_window(days_back: int, window_days: int) -> list[dict]:
    end = local_midnight_utc(days_back)
    start = local_midnight_utc(days_back + window_days)
    q = (
        f'SELECT restingHeartRate, totalSteps, stressPercentage, '
        f'bodyBatteryHighestValue, bodyBatteryLowestValue '
        f'FROM "DailyStats" WHERE time >= \'{start.isoformat()}\' AND time < \'{end.isoformat()}\' '
        f'AND "Device" = \'{WATCH_DEVICE}\' ORDER BY time DESC'
    )
    return influx_query(q)


def sleep_summary_window(days_back: int, window_days: int) -> list[dict]:
    """DailyStats.sleepingSeconds turned out to be unreliable (seen 5.7h on a
    night the Garmin app reported 7.5h/score 85) — SleepSummary.sleepTimeSeconds
    matches the app and comes with sleepScore, so use that instead. Its
    timestamp lands in the morning after the tracked night (once the watch
    finishes processing), not at local midnight, but still within the same
    local calendar day's window."""
    end = local_midnight_utc(days_back)
    start = local_midnight_utc(days_back + window_days)
    q = (
        f'SELECT sleepTimeSeconds, sleepScore '
        f'FROM "SleepSummary" WHERE time >= \'{start.isoformat()}\' AND time < \'{end.isoformat()}\' '
        f'AND "Device" = \'{WATCH_DEVICE}\' ORDER BY time DESC'
    )
    return influx_query(q)


def _body_battery_current() -> dict:
    """The daily high/low from DailyStats are day-summary values, not the current
    level — e.g. a fresh morning peak of 99 stays displayed as "today's battery"
    all day even after hours of draining, misleadingly far from what's actually
    on the watch right now. BodyBatteryIntraday has 5-min-interval readings, so
    the most recent one is a much closer (though not perfectly live — bounded by
    how recently the watch itself synced, see DeviceSync/wait_for_fresh_sync)
    approximation of "right now"."""
    rows = influx_query(
        f'SELECT BodyBatteryLevel FROM "BodyBatteryIntraday" WHERE "Device" = \'{WATCH_DEVICE}\' '
        f'ORDER BY time DESC LIMIT 1'
    )
    if not rows or rows[0].get("BodyBatteryLevel") is None:
        return {"available": False}
    row = rows[0]
    ts = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
    age_minutes = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    return {
        "available": True,
        "level": row["BodyBatteryLevel"],
        "age_minutes": round(age_minutes),
    }


def build_metrics() -> dict:
    today = daily_stats_window(0, 1)
    last_7d = daily_stats_window(0, 7)
    prev_7d = daily_stats_window(7, 7)
    last_28d = daily_stats_window(0, 28)

    today_sleep = sleep_summary_window(0, 1)
    today_sleep_h = round(today_sleep[0]["sleepTimeSeconds"] / 3600, 1) if today_sleep and today_sleep[0].get("sleepTimeSeconds") else None
    today_sleep_score = today_sleep[0].get("sleepScore") if today_sleep else None
    # sleep_vs_7d_avg compares against the 6 days BEFORE today, not including today
    # itself — otherwise the deviation gets structurally dampened by including
    # today in its own baseline average.
    prior_6d_sleep = sleep_summary_window(1, 6)
    prior_6d_sleep_avg = avg(prior_6d_sleep, "sleepTimeSeconds")
    last_7d_sleep = sleep_summary_window(0, 7)

    return {
        "today": {
            "resting_hr": today[0].get("restingHeartRate") if today else None,
            "steps": today[0].get("totalSteps") if today else None,
            "body_battery_current": _body_battery_current(),
            "body_battery_high": today[0].get("bodyBatteryHighestValue") if today else None,
            "body_battery_low": today[0].get("bodyBatteryLowestValue") if today else None,
            "sleep_hours": today_sleep_h,
            "sleep_score": today_sleep_score,
            "sleep_vs_prior_6d_avg_hours": round(today_sleep_h - prior_6d_sleep_avg / 3600, 1)
            if today_sleep_h is not None and prior_6d_sleep_avg
            else None,
            "activities_already_done_today": _activities_today(),
        },
        "last_7d_avg": {
            "resting_hr": avg(last_7d, "restingHeartRate"),
            "steps": avg(last_7d, "totalSteps"),
            "stress_pct": avg(last_7d, "stressPercentage"),
            "sleep_hours": round(avg(last_7d_sleep, "sleepTimeSeconds") / 3600, 1)
            if avg(last_7d_sleep, "sleepTimeSeconds")
            else None,
            "days_with_data": len(last_7d),
        },
        "prev_7d_avg": {
            "resting_hr": avg(prev_7d, "restingHeartRate"),
            "steps": avg(prev_7d, "totalSteps"),
            "stress_pct": avg(prev_7d, "stressPercentage"),
        },
        "last_28d_avg": {
            "resting_hr": avg(last_28d, "restingHeartRate"),
            "steps": avg(last_28d, "totalSteps"),
        },
        "training_readiness": _training_readiness(),
        "training_status": _training_status(),
        "lactate_threshold_running": _lactate_threshold(),
        "cycling_hr_zones": _cycling_hr_zones(today, last_28d),
        "cycling_power_zones": _cycling_power_zones(),
        "baseline_deviation": _baseline_deviation(),
        "recent_activities_14d": _activities_since(local_midnight_utc(LOG_HISTORY_DAYS - 1)),
        "intensity_distribution_7d": _intensity_distribution(7),
        "intensity_distribution_28d": _intensity_distribution(28),
        "training_load_by_sport": _training_load_by_sport(),
        "vo2max": _vo2max_trend(),
    }


def _activities_today() -> list[dict]:
    return _activities_since(local_midnight_utc(0))


def _activities_since(start: datetime) -> list[dict]:
    rows = influx_query(
        f'SELECT activityType, activityName, distance, elapsedDuration, averageHR, calories '
        f'FROM "ActivitySummary" WHERE time >= \'{start.isoformat()}\' ORDER BY time DESC'
    )
    result = []
    seen = set()
    for r in rows:
        activity_type = r.get("activityType")
        if not activity_type or activity_type == "No Activity":
            continue
        key = (r.get("time"), activity_type, r.get("activityName"), r.get("distance"))
        if key in seen:  # garmin-fetch-data can write an activity twice on re-sync
            continue
        seen.add(key)
        ts_utc = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
        ts_local = ts_utc.astimezone(LOCAL_TZ)
        result.append(
            {
                "date": r["time"][:10],
                "local_time": ts_local.strftime("%H:%M"),
                "type": activity_type,
                "name": r.get("activityName"),
                "distance_km": round(r["distance"] / 1000, 1) if r.get("distance") else None,
                "duration_min": round(r["elapsedDuration"] / 60) if r.get("elapsedDuration") else None,
                "avg_hr": r.get("averageHR"),
                "calories": r.get("calories"),
            }
        )
    return result


def _intensity_distribution(days: int) -> dict:
    """Polarized/pyramidal training theory: recreational endurance athletes do best
    with most volume easy (zone 1-2) and minimal time in the "grey zone" (zone 3) —
    the classic failure mode is easy days drifting into moderate effort. hrTimeInZone_*
    (seconds per activity) is already recorded per activity by garmin-fetch-data, so
    this is a straight sum-and-percentage over the window, no new data source needed."""
    start = local_midnight_utc(days - 1)
    rows = influx_query(
        f'SELECT hrTimeInZone_1, hrTimeInZone_2, hrTimeInZone_3, hrTimeInZone_4, hrTimeInZone_5 '
        f'FROM "ActivitySummary" WHERE time >= \'{start.isoformat()}\' AND "Device" = \'{WATCH_DEVICE}\''
    )
    zones = [0.0] * 5
    for r in rows:
        for i in range(5):
            zones[i] += r.get(f"hrTimeInZone_{i + 1}") or 0
    total = sum(zones)
    if total == 0:
        return {"available": False}
    low_pct = round((zones[0] + zones[1]) / total * 100)
    mid_pct = round(zones[2] / total * 100)
    high_pct = round((zones[3] + zones[4]) / total * 100)
    return {
        "available": True,
        "low_zone1_2_pct": low_pct,
        "mid_zone3_pct": mid_pct,
        "high_zone4_5_pct": high_pct,
        "total_hr_zone_minutes": round(total / 60),
    }


def _training_load_by_sport() -> dict:
    """Garmin's own ACWR (training_status.acute_chronic_load_ratio) combines running
    and cycling into one number, which can hide a sport-specific ramp (e.g. cycling
    load spiking while running stays flat). activityTrainingLoad is Garmin's own
    per-activity load figure (EPOC-based), already recorded — this sums it per sport
    over a 7d/28d-weekly-average window as a lighter per-sport signal alongside the
    combined ACWR, not a replacement for it (Garmin's ACWR uses different weighting
    internally, so don't call this figure "ACWR" too)."""
    start_7d = local_midnight_utc(6)
    start_28d = local_midnight_utc(27)
    result = {}
    for sport, types in SPORT_TYPES.items():
        type_filter = " OR ".join(f"activityType = '{t}'" for t in types)
        rows_7d = influx_query(
            f'SELECT activityTrainingLoad FROM "ActivitySummary" '
            f'WHERE time >= \'{start_7d.isoformat()}\' AND "Device" = \'{WATCH_DEVICE}\' AND ({type_filter})'
        )
        rows_28d = influx_query(
            f'SELECT activityTrainingLoad FROM "ActivitySummary" '
            f'WHERE time >= \'{start_28d.isoformat()}\' AND "Device" = \'{WATCH_DEVICE}\' AND ({type_filter})'
        )
        load_7d = sum(r.get("activityTrainingLoad") or 0 for r in rows_7d)
        load_28d = sum(r.get("activityTrainingLoad") or 0 for r in rows_28d)
        weekly_avg_28d = load_28d / 4
        result[sport] = {
            "load_last_7d": round(load_7d),
            "weekly_avg_last_28d": round(weekly_avg_28d),
            "load_ramp_ratio": round(load_7d / weekly_avg_28d, 2) if weekly_avg_28d else None,
        }
    return result


def _vo2max_trend() -> dict:
    """Latest known VO2max per sport plus the value from ~28 days ago for a trend —
    VO2_max_value (running) and VO2_max_value_cycling only update on days Garmin can
    compute them (not every activity), so pick the most recent non-null reading in
    each window rather than assuming the newest row has both."""
    def latest_nonnull(field: str, before_days: int = 0) -> float | None:
        end_clause = f'AND time < \'{local_midnight_utc(before_days).isoformat()}\'' if before_days else ""
        rows = influx_query(
            f'SELECT {field} FROM "VO2_Max" WHERE "Device" = \'{WATCH_DEVICE}\' {end_clause} '
            f'ORDER BY time DESC LIMIT 20'
        )
        for r in rows:
            if r.get(field) is not None:
                return r[field]
        return None

    running_now = latest_nonnull("VO2_max_value")
    running_28d_ago = latest_nonnull("VO2_max_value", before_days=28)
    cycling_now = latest_nonnull("VO2_max_value_cycling")
    cycling_28d_ago = latest_nonnull("VO2_max_value_cycling", before_days=28)

    result = {}
    if running_now is not None:
        result["running"] = {
            "current": running_now,
            "delta_28d": round(running_now - running_28d_ago, 1) if running_28d_ago is not None else None,
        }
    if cycling_now is not None:
        result["cycling"] = {
            "current": cycling_now,
            "delta_28d": round(cycling_now - cycling_28d_ago, 1) if cycling_28d_ago is not None else None,
        }
    return result or {"available": False}


def vo2max_series(days: int) -> dict:
    """Actual reading-by-reading VO2max history for the dashboard's trend chart —
    _vo2max_trend() above only keeps two points (now vs. ~28d ago) for the LLM
    prompt, which isn't enough to draw a real line. Only non-null readings are
    returned (both fields are sparse — see _vo2max_trend's docstring)."""
    start = local_midnight_utc(days - 1)
    result = {}
    for sport, field in (("running", "VO2_max_value"), ("cycling", "VO2_max_value_cycling")):
        rows = influx_query(
            f'SELECT {field} FROM "VO2_Max" WHERE "Device" = \'{WATCH_DEVICE}\' '
            f'AND time >= \'{start.isoformat()}\' ORDER BY time ASC'
        )
        result[sport] = [{"date": r["time"][:10], "value": r[field]} for r in rows if r.get(field) is not None]
    return result


def _stddev(vals: list[float]) -> float | None:
    if len(vals) < 2:
        return None
    mean_val = sum(vals) / len(vals)
    variance = sum((v - mean_val) ** 2 for v in vals) / (len(vals) - 1)
    return variance**0.5


def _baseline_deviation() -> dict:
    """Compares today's HRV and resting HR against a 28-day rolling baseline
    (mean + stddev). Garmin's own training_readiness score already folds
    similar signals together as a black box — this gives the LLM an explicit,
    concrete number to reference instead ("HRV is 1.3 SD below your 28-day
    baseline") rather than only a bare 0-100 score."""
    today_hrv_rows = influx_query("SELECT mean(hrvValue) FROM HRV_Intraday WHERE time > now() - 1d")
    today_hrv = today_hrv_rows[0].get("mean") if today_hrv_rows else None

    daily_hrv_rows = influx_query(
        "SELECT mean(hrvValue) FROM HRV_Intraday WHERE time > now() - 29d AND time < now() - 1d "
        "GROUP BY time(1d) fill(none)"
    )
    hrv_history = [r["mean"] for r in daily_hrv_rows if r.get("mean") is not None]

    rhr_rows = daily_stats_window(1, 28)
    rhr_history = [r["restingHeartRate"] for r in rhr_rows if r.get("restingHeartRate") is not None]
    today_rhr_rows = daily_stats_window(0, 1)
    today_rhr = today_rhr_rows[0].get("restingHeartRate") if today_rhr_rows else None

    result = {}
    if today_hrv is not None and len(hrv_history) >= 7:
        hrv_mean = sum(hrv_history) / len(hrv_history)
        hrv_sd = _stddev(hrv_history)
        result["hrv_today"] = round(today_hrv, 1)
        result["hrv_28d_baseline_mean"] = round(hrv_mean, 1)
        result["hrv_deviation_sd"] = round((today_hrv - hrv_mean) / hrv_sd, 1) if hrv_sd else None

    if today_rhr is not None and len(rhr_history) >= 7:
        rhr_mean = sum(rhr_history) / len(rhr_history)
        rhr_sd = _stddev(rhr_history)
        result["resting_hr_today"] = today_rhr
        result["resting_hr_28d_baseline_mean"] = round(rhr_mean, 1)
        result["resting_hr_deviation_sd"] = round((today_rhr - rhr_mean) / rhr_sd, 1) if rhr_sd else None

    return result or {"available": False}


def _training_readiness() -> dict:
    r = latest("TrainingReadiness")
    if not r.get("available", True):
        return r
    return {
        "score": r.get("score"),
        "level": r.get("level"),
        "acute_load": r.get("acuteLoad"),
        "recovery_time_hours": round(r["recoveryTime"] / 60, 1) if r.get("recoveryTime") else None,
        "sleep_score": r.get("sleepScore"),
        # Garmin's own per-factor breakdown of the score (0-100 each, higher = more
        # favorable) — partially de-black-boxes the composite score, and is the key
        # to diagnosing a disagreement with baseline_deviation (does a low readiness
        # trace back to HRV specifically, or to sleep/stress/load instead?).
        "factor_hrv": r.get("hrvFactorPercent"),
        "factor_sleep": r.get("sleepScoreFactorPercent"),
        "factor_recovery_time": r.get("recoveryTimeFactorPercent"),
        "factor_acwr": r.get("acwrFactorPercent"),
        "factor_stress_history": r.get("stressHistoryFactorPercent"),
        "data_stale": r.get("_stale", False),
    }


def _training_status() -> dict:
    r = latest("TrainingStatus")
    if not r.get("available", True):
        return r
    return {
        "status": r.get("trainingStatusFeedbackPhrase"),
        "acute_chronic_load_ratio": r.get("dailyAcuteChronicWorkloadRatio"),
        "acute_load": r.get("dailyTrainingLoadAcute"),
        "chronic_load": r.get("dailyTrainingLoadChronic"),
        "data_stale": r.get("_stale", False),
    }


def _format_pace(decimal_min_per_km: float) -> str:
    total_seconds = round(decimal_min_per_km * 60)
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def _lactate_threshold() -> dict:
    r = latest("LactateThreshold")
    if not r.get("available", True):
        return r
    # Garmin's lactateThresholdSpeed endpoint returns the value as seconds/meter (pace),
    # not m/s speed — verified against a realistic threshold pace (~5-6 min/km).
    sec_per_m = r.get("SpeedThreshold_RUNNING")
    pace_decimal = sec_per_m * 1000 / 60 if sec_per_m else None
    return {
        "threshold_hr_running": r.get("HeartRateThreshold_RUNNING"),
        "threshold_pace_min_per_km": f"{_format_pace(pace_decimal)} min/km" if pace_decimal else None,
        "data_stale": r.get("_stale", False),
    }


def _cycling_hr_zones(today: list[dict], last_28d: list[dict]) -> dict:
    # No cycling-specific lactate threshold available via Garmin's API (the endpoint
    # exists technically but returns no data for this account) — compute generic HR
    # zones with the Karvonen formula instead of a real FTP/threshold test. Kept as a
    # fallback for when _cycling_power_zones() has no data (e.g. a new install with
    # no long laps yet); when power data is available, that's the primary zone source.
    resting_hr = (today[0].get("restingHeartRate") if today else None) or avg(last_28d, "restingHeartRate")
    # Only cycling activities for max HR — running typically gives 5-10 bpm higher
    # peaks and would skew the cycling zones.
    cycling_filter = " OR ".join(f"activityType = '{t}'" for t in SPORT_TYPES["cycling"])
    max_hr_rows = influx_query(f"SELECT max(maxHR) FROM ActivitySummary WHERE time > now() - 90d AND ({cycling_filter})")
    max_hr = max_hr_rows[0].get("max") if max_hr_rows else None
    if not resting_hr or not max_hr:
        return {"available": False}
    reserve = max_hr - resting_hr
    return {
        "available": True,
        "resting_hr": resting_hr,
        "max_hr_measured_90d_cycling": max_hr,
        "zone2_endurance_bpm": [round(resting_hr + reserve * 0.6), round(resting_hr + reserve * 0.7)],
        "zone3_tempo_bpm": [round(resting_hr + reserve * 0.7), round(resting_hr + reserve * 0.8)],
        "zone4_threshold_bpm": [round(resting_hr + reserve * 0.8), round(resting_hr + reserve * 0.9)],
        "zone5_vo2max_bpm": [round(resting_hr + reserve * 0.9), max_hr],
    }


FTP_MIN_LAP_SECONDS = 18 * 60  # long enough to be a meaningful sustained-effort proxy for a 20-min FTP test
FTP_FROM_20MIN_FACTOR = 0.95  # standard estimate: FTP ≈ 95% of best ~20-min average power


def _cycling_power_zones() -> dict:
    """Estimates FTP from the single best sustained-power lap (>=18 min) across the
    last 90 days of cycling activities, using the standard "95% of best ~20-min
    power" rule of thumb — no dedicated FTP test needed, just real ride data the
    watch/bike computer already recorded per lap. This is more direct than the HR-based
    Karvonen zones in _cycling_hr_zones() (which only approximates effort via heart
    rate), but it's still an estimate from whatever laps happen to exist, not a
    structured test — label it as such in the prompt."""
    # ActivityLap uses its own coarse Sport field ("cycling"/"running"/"generic"), unlike
    # ActivitySummary's fine-grained activityType (road_biking/gravel_cycling/...) that
    # SPORT_TYPES is built for — a different field, not reusable here.
    rows = influx_query(
        f'SELECT Avg_Power, Elapsed_Time FROM "ActivityLap" WHERE time > now() - 90d '
        f'AND Sport = \'cycling\' AND "Device" = \'{WATCH_DEVICE}\''
    )
    candidates = [
        r["Avg_Power"]
        for r in rows
        if r.get("Avg_Power") is not None and r.get("Elapsed_Time") is not None and r["Elapsed_Time"] >= FTP_MIN_LAP_SECONDS
    ]
    if not candidates:
        return {"available": False}
    best_power = max(candidates)
    ftp = round(best_power * FTP_FROM_20MIN_FACTOR)
    # Standard 6-zone model (Coggan), expressed as % of FTP.
    return {
        "available": True,
        "estimated_ftp_watts": ftp,
        "estimated_from_best_lap_watts": best_power,
        "note": "estimated from the best sustained (18+ min) lap in the last 90 days, not a dedicated FTP test",
        "zone1_recovery_watts": [0, round(ftp * 0.55)],
        "zone2_endurance_watts": [round(ftp * 0.56), round(ftp * 0.75)],
        "zone3_tempo_watts": [round(ftp * 0.76), round(ftp * 0.90)],
        "zone4_threshold_watts": [round(ftp * 0.91), round(ftp * 1.05)],
        "zone5_vo2max_watts": [round(ftp * 1.06), round(ftp * 1.20)],
        "zone6_anaerobic_watts": [round(ftp * 1.21), None],
    }


_COLOR_RANK = {"red": 3, "yellow": 2, "green": 1, "gray": 0}


def _compute_sport_adherence(target: dict | None, actual_activities: list[dict], sport: str) -> dict:
    """Compares a structured run_target/bike_target (written the day the advice was
    given) against what was actually logged that calendar day. See SPORT_TYPES for
    the sport->activityType grouping (shared with _training_load_by_sport)."""
    matching = [a for a in actual_activities if a.get("type") in SPORT_TYPES[sport]]
    any_activity = bool(actual_activities)

    if target is None:
        color = "yellow" if matching or any_activity else "gray"
        return {"color": color, "target": None, "actual_duration_min": None, "actual_avg_hr": None}

    if not matching:
        color = "yellow" if any_activity else "red"
        return {"color": color, "target": target, "actual_duration_min": None, "actual_avg_hr": None}

    total_duration = sum(a.get("duration_min") or 0 for a in matching)
    hr_values = [(a.get("duration_min") or 0, a.get("avg_hr")) for a in matching if a.get("avg_hr") is not None]
    weighted_hr = (
        round(sum(d * hr for d, hr in hr_values) / sum(d for d, hr in hr_values), 1)
        if hr_values and sum(d for d, hr in hr_values) > 0
        else None
    )

    duration_ratio = total_duration / target["duration_min"] if target.get("duration_min") else None
    hr_ok = weighted_hr is not None and (target["hr_min"] - 5) <= weighted_hr <= (target["hr_max"] + 5)
    duration_ok = duration_ratio is not None and 0.75 <= duration_ratio <= 1.35

    color = "green" if duration_ok and hr_ok else "yellow"
    return {
        "color": color,
        "target": target,
        "actual_duration_min": total_duration,
        "actual_avg_hr": weighted_hr,
    }


def _compute_adherence(target_entry: dict, actual_activities: list[dict]) -> dict:
    advice = target_entry.get("advice", {})
    run = _compute_sport_adherence(advice.get("run_target"), actual_activities, "running")
    bike = _compute_sport_adherence(advice.get("bike_target"), actual_activities, "cycling")
    day_color = max((run["color"], bike["color"]), key=lambda c: _COLOR_RANK[c])
    return {"run": run, "bike": bike, "day_color": day_color}


def _backfill_adherence(entries: list[dict]) -> list[dict]:
    """Yesterday's advice targeted "today" at write time, so it can only be scored
    once today's activities actually exist — done here, one cron run later, rather
    than at dashboard-read time, so the algorithm lives in one place and the result
    is persisted (no recomputation drift)."""
    yesterday = NOW_LOCAL.date() - timedelta(days=1)
    for entry in entries:
        if entry.get("weekly") or "adherence" in entry:
            continue
        if entry.get("date") != yesterday.isoformat():
            continue
        actual = [a for a in _activities_since(local_midnight_utc(1)) if a.get("date") == yesterday.isoformat()]
        entry["adherence"] = _compute_adherence(entry, actual)
    return entries


def read_coach_log() -> list[dict]:
    """Own log of past advice, kept on the persisted coach-home volume — the
    Claude session itself is reset (`/clear`) after every run, so without this
    each day starts from zero with no memory of what was advised or how the
    user's ACWR/readiness trended over the past runs."""
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        entries = json.load(f)
    cutoff = (NOW_LOCAL - timedelta(days=LOG_HISTORY_DAYS)).date().isoformat()
    return [e for e in entries if e.get("date", "") >= cutoff]


def write_coach_log(advice: dict, weekly: bool):
    entries = _backfill_adherence(read_coach_log())
    entries.append(
        {
            "date": NOW_LOCAL.date().isoformat(),
            "weekly": weekly,
            "advice": advice,
        }
    )
    with open(LOG_FILE, "w") as f:
        json.dump(entries, f, ensure_ascii=False)


TIP_STRUCTURE = """TIP STRUCTURE — applies to run_tip, bike_tip, and to the weekly run_advice and bike_advice. Every one of these fields MUST follow this exact structure, in {LANGUAGE}:

Write exactly two blocks separated by a single line break ("\\n"). No headers, no bullets, no bold.

BLOCK 1 — THE CALL. One short sentence (max ~15 words). State the decision first, before any reasoning. Two allowed forms, nothing else:
- Session advised: name the session type, duration, and intensity target, matching the run_target/bike_target numbers exactly. Example shape: "Easy run today: 40 min at HR 130-148."
- No session advised: state that explicitly as a decision, optionally with the reason in three words. Example shape: "No second ride today — this morning covers it." Never skip Block 1 or replace it with reasoning when there is no session; "do nothing" is a call and must be stated as one.

BLOCK 2 — THE WHY, then at most ONE watch-out. Two sentences maximum.
- Sentence 1: the evidence for the call. Cite at most 3 concrete values from the data (dates, distances, HR, ACWR, HRV, zone percentages). Every value must come from the provided data — never invent numbers.
- Sentence 2 (optional): exactly one forward-looking caveat or execution cue, also grounded in a real number. If you have nothing important, omit it. Never add a second caveat.

HARD RULES:
- The order is always: decision -> evidence -> caveat. Never open with a fact or reasoning ("You haven't run yet...", "Your ACWR is..."); open with what to do.
- run_tip and bike_tip must be structurally parallel: same two-block shape, same sentence roles, so they read as one coach speaking.
- Do not repeat a number already used in Block 1 inside Block 2 unless it adds new meaning.
- Keep each full tip under 500 characters. Plain declarative sentences, no wordplay or idioms — the text must translate cleanly into any language."""

JSON_SCHEMA_DAILY = """{
  "status": "<1 sentence: sleep/resting HR/body battery/training readiness score>",
  "run_tip": "<follows TIP STRUCTURE above>",
  "bike_tip": "<follows TIP STRUCTURE above>",
  "run_target": "<{duration_min: int, hr_min: int, hr_max: int} or null if no run session advised today>",
  "bike_target": "<{duration_min: int, hr_min: int, hr_max: int} or null if no bike session advised today>",
  "tomorrow_run": "<1 tentative one-liner: workout type + duration + HR range for tomorrow's run, e.g. 'Likely an easy run, ~35 min, HR 130-148' — or 'Likely a rest day' if no run is probable. Null if genuinely too uncertain to say anything.>",
  "tomorrow_bike": "<1 tentative one-liner: workout type + duration + power range (from cycling_power_zones if available, otherwise HR range) for tomorrow's ride, e.g. 'Likely a tempo ride, ~45 min, 175-210W' — or 'Likely a rest day' if no ride is probable. Null if genuinely too uncertain to say anything.>",
  "color": "green or yellow"
}"""

JSON_SCHEMA_WEEKLY = """{
  "performance": "<2-3 sentences overall weekly assessment, referencing the trend across recent_activities_14d, coach_log, vo2max, and intensity_distribution_7d/28d>",
  "recovery": "<2-3 sentences, explicitly mention the ACWR ratio and training_load_by_sport>",
  "run_advice": "<follows TIP STRUCTURE above, applied to the coming week's plan instead of just today>",
  "bike_advice": "<follows TIP STRUCTURE above, applied to the coming week's plan instead of just today>",
  "watch_point": "<1-2 sentences, or 'Nothing notable'>",
  "color": "green, orange or red"
}"""


def build_prompt(metrics: dict, weekly: bool, coach_log: list[dict]) -> str:
    m = json.dumps(metrics, indent=2, ensure_ascii=False)
    log = json.dumps(coach_log, indent=2, ensure_ascii=False) if coach_log else "[]"
    disclaimer = (
        "No medical claims or diagnoses. For persistent issues, refer to a doctor "
        "or physiotherapist — avoid overly confident statements."
    )
    context = """Field explanations:
- training_readiness.score (0-100) and .level: Garmin's own readiness score for today.
  If "available": false or "data_stale": true is set: this data is missing or stale
  (more than 36 hours old) — don't rely on it; fall back on baseline_deviation, sleep and ACWR.
  training_readiness.factor_hrv / factor_sleep / factor_recovery_time / factor_acwr /
  factor_stress_history (0-100 each, higher = more favorable) are Garmin's own breakdown of
  what's driving the score — use them to explain WHY readiness is high or low, and to check
  whether a disagreement with baseline_deviation traces back to HRV itself or to a non-HRV
  factor (sleep, stress history, recovery time).
- training_status.acute_chronic_load_ratio (ACWR): <0.8 = too little load (undertrained),
  0.8-1.3 = healthy zone, 1.3-1.5 = caution (ramping up), >1.5 = ramping up fast — treat
  these as a load-ramp guardrail (how fast load is rising vs. your recent average), not as
  a direct injury prediction; the science behind the original "injury risk" framing has been
  formally challenged, so don't state it as established fact. This covers overall load
  (running + cycling combined) and is the PRIMARY figure for "should today involve hard
  training or not" — the body has one recovery system regardless of how many sports produced
  the load, so this combined number always takes priority over the per-sport figures below.
- training_load_by_sport.<running|cycling>.load_ramp_ratio: this week's training-load sum for
  that sport specifically, divided by its 4-week weekly average — same interpretation bands as
  ACWR above, but per sport (Garmin's own ACWR is combined). This is a SECONDARY, diagnostic
  signal, not a second recovery gate — use it only to explain WHICH sport is driving a change
  (e.g. "the combined ACWR is fine, but running specifically ramped up fast, so keep that one
  sport conservative even though cycling volume could handle more"), never to override or add
  to the combined ACWR's verdict on overall training load. Don't call this figure "ACWR" — it's
  a lighter, differently-weighted approximation using Garmin's per-activity load number.
  null means not enough load in the last 28 days to compute a meaningful ratio. Treat a very
  high ratio (e.g. >3) with caution if weekly_avg_last_28d is small (e.g. under ~30) — with a
  low, sparse baseline (that sport trained only occasionally) a single session this week can
  produce a misleadingly extreme ratio; describe it as "picked up this sport again" rather than
  a dramatic load spike in that case.
- training_status.status: Garmin's classification (e.g. PRODUCTIVE, PEAKING, OVERREACHING, RECOVERY).
- intensity_distribution_7d / intensity_distribution_28d: percentage of all heart-rate-zone
  time (across all activities in the window) spent low (zone 1-2, easy), mid (zone 3, "grey
  zone" — neither properly easy nor a real hard effort), or high (zone 4-5). Recreational
  endurance athletes generally do best with a polarized/pyramidal distribution: mostly easy
  volume (roughly 75-80%+ low), a small high-intensity portion, and minimal zone 3 — zone 3
  creep (easy days drifting into moderate effort) is a common, coachable mistake. If
  mid_zone3_pct is notably elevated (e.g. above ~20-25%) and there's been no explicit hard
  session logged, mention it as a pattern to watch, especially in the weekly review. If
  "available": false, not enough HR-zone data in the window yet.
- vo2max.running / vo2max.cycling: current estimated VO2max and the change over the last 28
  days (delta_28d), when available — a slow multi-week rise is a good sign of improving
  aerobic fitness, a drop of more than ~1-2 points can reflect detraining or incomplete
  recovery. Don't over-interpret single-day noise; this is more useful as a slow trend than
  a daily signal, so mention it mainly in the weekly review unless it moved sharply.
- lactate_threshold_running.threshold_hr_running: exact, measured heart rate threshold for running.
- lactate_threshold_running.threshold_pace_min_per_km: exact, measured threshold pace for running,
  ALREADY FORMATTED as "M:SS min/km" (e.g. "5:41 min/km") — reuse this notation verbatim,
  don't recalculate or interpret it as a decimal number.
- cycling_power_zones: PREFERRED source for cycling intensity targets when "available": true.
  estimated_ftp_watts is derived from the single best sustained (18+ min) power lap in the last
  90 days, using the standard "FTP ≈ 95% of best ~20-min power" rule — a real estimate from
  actual ride data, not a guess, but also not a dedicated FTP test, so phrase cycling advice in
  watts using these zones and mention once that the FTP is estimated from recent rides (not a
  formal test). zone1-6 are the standard % of FTP power zones (recovery/endurance/tempo/
  threshold/VO2max/anaerobic); zone6's upper bound is intentionally unbounded (null).
- cycling_hr_zones: fallback ONLY — use this for cycling intensity targets when
  cycling_power_zones is NOT available (e.g. not enough long laps yet). These are generic,
  calculated heart rate zones (Karvonen formula based on resting HR and measured max HR during
  cycling activities), less precise than a power-based estimate. Mention this fallback status
  when using it. Never mix watts and this HR zone in the same tip — pick one source per tip
  based on what's available.
- today.sleep_hours, today.sleep_score (Garmin's own 0-100 sleep quality score) and
  today.sleep_vs_prior_6d_avg_hours (difference vs. the average of the preceding 6 days, so
  not counting today itself): sleep is a FULL decision factor alongside readiness/ACWR, not
  just a status figure. A clear deficit (e.g. -1.5h or more) or a low sleep_score should
  noticeably temper the intensity/duration of the advice, even if body battery/readiness look
  fine on their own — a high body battery after little sleep mostly reflects current energy,
  not full physical recovery.
- today.activities_already_done_today: sport activities ALREADY completed since local midnight
  today (type, name, distance, duration in minutes, avg heart rate, calories, local_time —
  the clock time, e.g. "05:55", the activity started). If you mention time of day (morning/
  afternoon/evening) for a session, base it strictly on local_time — never assume or guess
  "this morning" just because it's the first/only session shown; check the actual clock time
  (roughly: before 12:00 = morning, 12:00-18:00 = afternoon, after 18:00 = evening). This
  applies equally when describing multiple sessions the same day (e.g. one at 05:55 and one at
  13:50 is "one this morning, one this afternoon", not "two rides this morning"). The user
  regularly trains the same sport twice in one day (e.g. a short morning ride + a longer
  evening ride) — "already done once" does NOT automatically mean "done for today". Assess
  per sport whether another session today is still reasonable:
  * If the completed session was short/light (e.g. <45 min, low avg HR relative to the
    threshold HR) and readiness/ACWR otherwise look good: a 2nd, additional session is fine —
    treat it as a normal training day with a possibly lighter 2nd session (e.g. a short
    endurance ride/run or recovery session), not as "extra on top of an already full day".
  * If the completed session was long/hard (e.g. >90 min, or avg HR close to/above the
    threshold HR), or there are already 2+ sessions of the same sport: advise against another
    session of that sport, give at most a short recovery tip, and explicitly mention what was
    already done (type/duration/distance).
  For the sport not done at all today: give normal advice. If the list is empty: give a
  suggestion for both sports.
- run_target / bike_target: structured version of your run_tip/bike_tip advice, used to
  later check whether the plan was actually followed. Set to null if you are not advising
  a session for that sport today (e.g. rest day, or already covered under
  activities_already_done_today). Otherwise: {"duration_min": <int>, "hr_min": <int>,
  "hr_max": <int>} — duration_min is your intended session length in minutes, hr_min/hr_max
  is the target average-heart-rate RANGE for that session (not a hard ceiling — an average
  across the whole session), used for tracking adherence afterwards. Always fill this with a
  HR range even when bike_tip's headline intensity is given in watts (from
  cycling_power_zones) — convert/estimate the equivalent HR range for the target power zone so
  adherence can still be tracked from recorded heart rate. These must be consistent with what
  you just wrote in run_tip/bike_tip, not a separate or vaguer suggestion — e.g. if run_tip
  says "40 minute easy run, HR 130-150", run_target must be
  {"duration_min": 40, "hr_min": 130, "hr_max": 150}.
- recent_activities_14d: full activity history of the last 14 days (date, local_time, type,
  duration, avg HR), NOT necessarily one entry per day — there can be, and often are, rest days with no
  entry at all in between. Use this to vary the workout type sensibly instead of suggesting the
  same generic endurance session every day — e.g. if there's been no intensity in several days
  and readiness/ACWR allow it, a tempo or interval session is appropriate; if there have already
  been 2+ hard sessions this week, favor endurance/recovery instead. Reference concrete
  sessions from this list when relevant (e.g. "after yesterday's 12km tempo run..."). Never
  state a count of consecutive days ("Nth day in a row") unless you have actually checked the
  `date` field of every entry and confirmed there is no gap — when in doubt, just describe the
  pattern qualitatively (e.g. "frequent cycling load this week") instead of inventing a number.
- coach_log: your own last 14 days of advice (JSON, oldest first). Use this to
  stay consistent (don't contradict yesterday's plan without reason) and to build an actual
  short-term plan across days (e.g. if you suggested an easy day yesterday, today can pick up
  intensity again, referencing that). If empty, this is one of the first runs — say so is not
  necessary, just give standalone advice.
- tomorrow_run / tomorrow_bike (daily only): brief, tentative one-liners for tomorrow, one per
  sport, reasoning forward from today's plan and recent load — e.g. if today is an easy/recovery
  day for a sport, tomorrow likely has room for intensity in that sport; if today includes a
  hard session, tomorrow is probably easier or a rest day for that sport. Each one-liner must
  name a workout type (easy/tempo/interval/rest) AND include a concrete duration + intensity
  range so the user can mentally prepare (e.g. "Likely an easy run, ~35 min, HR 130-148" or, for
  cycling, prefer watts over HR when cycling_power_zones is available: "Likely a tempo ride,
  ~45 min, 175-210W"). Keep the WORDING tentative ("likely", not a firm commitment) even though
  the numbers are concrete — tomorrow's actual advice will be generated fresh with tomorrow's
  real data (sleep, HRV, whether today's plan was actually followed), so this is same-day
  mental preparation, not a promise to be held to. If a rest day is likely for a sport, say so
  plainly (e.g. "Likely a rest day") rather than inventing a session. Set to null only if
  genuinely too uncertain to say anything useful (e.g. very early in the coach_log history).
- baseline_deviation.hrv_deviation_sd / resting_hr_deviation_sd: today's HRV/resting HR
  expressed as standard deviations from your 28-day rolling baseline. Roughly: within ±1 SD
  is normal day-to-day variation, beyond ±1 SD is a notable deviation worth mentioning,
  beyond ±2 SD is a strong signal (HRV notably below baseline or RHR notably above baseline
  both suggest incomplete recovery). IMPORTANT — relationship to training_readiness: HRV also
  feeds into Garmin's readiness score above, so these are two partially overlapping views of
  the same underlying recovery state, NOT two independent signals. Never cite them as separate
  pieces of evidence for the same conclusion — when they agree, lead with the explicit SD
  number as your cited evidence and mention the readiness score at most once as confirmation,
  without escalating the severity because "both" say so. When they clearly disagree (e.g.
  readiness MODERATE or better while hrv_deviation_sd is at or below -2 or
  resting_hr_deviation_sd at or above +2 — or readiness LOW while both deviations are within
  ±1 SD), do both of the following: (1) let the MORE CAUTIOUS of the two cap today's
  intensity — never use the more optimistic signal to argue a warning away; (2) name the
  disagreement explicitly in the tip so the user can see the signals diverged. One nuance: a
  single night's HRV is noisier than Garmin's multi-night smoothing, so a lone deviation
  between 1 and 2 SD against an otherwise good readiness score justifies caution in intensity,
  not automatically a full rest day. If "available": false, this data isn't available yet
  (needs at least 7 days of history)."""

    recovery_note = (
        "If running/cycling is discouraged today (low readiness, high ACWR, or training_status "
        "on RECOVERY/OVERREACHING): give a short walking suggestion instead (e.g. 20-30 min easy "
        "walk) as an active-recovery alternative — that's not a contradiction of resting, active "
        "recovery promotes blood flow without adding load. State explicitly that it's a recovery "
        "day, not a training day."
    )

    schema = JSON_SCHEMA_WEEKLY if weekly else JSON_SCHEMA_DAILY
    role = (
        'You are an experienced fitness/recovery coach, similar to Garmin\'s own '
        '"Daily Suggested Workouts" feature but with more context and explanation.'
        if weekly
        else 'You are a fitness coach who, like Garmin\'s "Daily Suggested Workouts", gives '
        'concrete, actionable training advice for TODAY — not just "take it easy" or a bare '
        'HR range, but a specific workout type (endurance run, tempo, intervals, recovery, '
        'rest), with duration and intensity target, chosen based on recent training history '
        'and what was advised previously — not a generic template repeated every day.'
    )
    comparison_note = (
        "\n`last_7d_avg` = this week, `prev_7d_avg` = last week, `last_28d_avg` = 4-week "
        "baseline. Explicitly compare trends (improvement/decline), not just a snapshot.\n"
        if weekly
        else ""
    )

    return f"""{role} Always give running and cycling advice in separate fields, never mixed
into one. Be evidence-based: every recommendation must reference a specific number from the
data below (e.g. "ACWR is 0.9" or "HRV is 1.3 SD below your baseline"), not vague statements
like "your body seems tired".
{recovery_note}

Use this Garmin data (already pre-computed — no raw time series, don't use any numbers other
than what's given below):

{m}

Your own advice from the last {LOG_HISTORY_DAYS} days (coach_log, oldest first):

{log}

{context}
{comparison_note}
{TIP_STRUCTURE.format(LANGUAGE=LANGUAGE)}

Respond with valid JSON per this schema. Write all text values in {LANGUAGE}, EXCEPT the
"color" field — always keep that in English exactly as shown in the schema (green/yellow/
orange/red), regardless of {LANGUAGE}:
{schema}

{disclaimer}

Write ONLY the JSON (nothing else, no explanation, no markdown code block) to the file
{OUTPUT_FILE} using the {get_provider(PROVIDER)["write_tool_name"]} tool. Then give a brief confirmation in the chat."""


def call_claude(prompt: str) -> dict:
    """Sends the prompt to the permanent 'coach' tmux session (running whichever
    provider is selected — see providers.py) instead of spawning a fresh
    headless CLI subprocess per call — the latter triggers an expensive cache
    write (system prompt/tools) every time, which burns a disproportionate
    amount of the session quota on a cold/fresh container start.

    The CLI writes the answer itself to OUTPUT_FILE (via its file-write tool)
    instead of us trying to scrape it from the tmux screen text — screen-text
    parsing turned out to be fragile (race conditions with intermediate
    'thinking' frames, line wraps that broke JSON strings, markers seen too
    early/late)."""
    import session_ask

    session_ask.ask_and_wait_for_file(prompt, OUTPUT_FILE)
    with open(OUTPUT_FILE) as f:
        response_text = f.read().strip()
    os.remove(OUTPUT_FILE)

    if response_text.startswith("```"):
        response_text = response_text.strip("`")
        if response_text.startswith("json"):
            response_text = response_text[4:]
        response_text = response_text.strip()
    return json.loads(response_text)


# Matched against English color-name substrings so this keeps working regardless
# of LANGUAGE — Claude is instructed to write the "color" value in English
# (see JSON_SCHEMA_*), only the advice text itself is translated.
COLOR_MAP = {"green": 0x2ECC71, "yellow": 0xF1C40F, "orange": 0xE67E22, "red": 0xE74C3C}


def _field(value: str | None) -> str:
    value = (value or "-").strip()
    return value[:1024] if value else "-"


def post_discord(embed: dict):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("No Discord webhook URL configured — set one on the dashboard's Settings page.")
    body = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord post failed: {resp.status}")


def _footer_text() -> str:
    return f"Garmin AI Coach — {get_provider(PROVIDER)['label']}"


def build_embed(advice: dict, weekly: bool) -> dict:
    color = COLOR_MAP.get(str(advice.get("color", "")).lower(), 0x95A5A6)
    today_str = NOW_LOCAL.strftime("%d-%m-%Y")

    if weekly:
        return {
            "title": f"🏃🚴 Weekly overview — {today_str}",
            "color": color,
            "fields": [
                {"name": "📊 Performance", "value": _field(advice.get("performance")), "inline": False},
                {"name": "😴 Recovery", "value": _field(advice.get("recovery")), "inline": False},
                {"name": "🏃 Running", "value": _field(advice.get("run_advice")), "inline": False},
                {"name": "🚴 Cycling", "value": _field(advice.get("bike_advice")), "inline": False},
                {"name": "⚠️ Watch point", "value": _field(advice.get("watch_point")), "inline": False},
            ],
            "footer": {"text": _footer_text()},
        }
    fields = [
        {"name": "Status", "value": _field(advice.get("status")), "inline": False},
        {"name": "🏃 Running", "value": _field(advice.get("run_tip")), "inline": False},
        {"name": "🚴 Cycling", "value": _field(advice.get("bike_tip")), "inline": False},
    ]
    if advice.get("tomorrow_run") or advice.get("tomorrow_bike"):
        preview_lines = []
        if advice.get("tomorrow_run"):
            preview_lines.append(f"🏃 {advice['tomorrow_run']}")
        if advice.get("tomorrow_bike"):
            preview_lines.append(f"🚴 {advice['tomorrow_bike']}")
        fields.append({"name": "🔭 Tomorrow (preview)", "value": _field("\n".join(preview_lines)), "inline": False})
    return {
        "title": f"📅 Today — {today_str}",
        "color": color,
        "fields": fields,
        "footer": {"text": _footer_text()},
    }


def post_error_to_discord(error: Exception):
    embed = {
        "title": "⚠️ Garmin AI Coach — failed",
        "color": 0x95A5A6,
        "description": f"```{str(error)[:1900]}```",
        "footer": {"text": _footer_text()},
    }
    try:
        post_discord(embed)
    except Exception:
        pass  # if even the error message can't be sent, there's nothing more to do


SYNC_WAIT_MAX_SECONDS = 180
SYNC_WAIT_POLL_SECONDS = 60


def wait_for_fresh_sync():
    """garmin-fetch-data syncs from Garmin Connect to InfluxDB every 5 minutes
    on its own — this just gives it a bit of headroom before we read the data,
    in case a sync cycle is in progress right when this runs. Not a hard
    guarantee (we don't have Docker access to force a sync), just a short,
    bounded wait: if the watch's last-known sync (DeviceSync) is very recent,
    give garmin-fetch-data one more poll interval to pick it up before we read."""
    rows = influx_query("SELECT * FROM DeviceSync ORDER BY time DESC LIMIT 1")
    if not rows:
        return
    ts = datetime.fromisoformat(rows[0]["time"].replace("Z", "+00:00"))
    age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    if age_seconds < SYNC_WAIT_POLL_SECONDS:
        wait = min(SYNC_WAIT_POLL_SECONDS - age_seconds + 5, SYNC_WAIT_MAX_SECONDS)
        print(f"Recent watch sync detected ({age_seconds:.0f}s ago) — waiting {wait:.0f}s for garmin-fetch-data to catch up.")
        time.sleep(wait)


def main():
    wait_for_fresh_sync()
    metrics = build_metrics()
    coach_log = read_coach_log()
    prompt = build_prompt(metrics, IS_WEEKLY, coach_log)
    advice = call_claude(prompt)
    embed = build_embed(advice, IS_WEEKLY)
    post_discord(embed)
    write_coach_log(advice, IS_WEEKLY)
    print(f"Done ({'weekly' if IS_WEEKLY else 'daily'}):", json.dumps(advice, ensure_ascii=False))


if __name__ == "__main__":
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another coach.py run is already in progress — skipping.", file=sys.stderr)
        sys.exit(1)
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        post_error_to_discord(e)
        sys.exit(1)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
