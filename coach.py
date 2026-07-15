#!/usr/bin/env python3
"""Garmin AI coach: queries InfluxDB, aggregates metrics, asks Claude Code for
advice, posts the result to Discord. Runs daily; on Sundays does a full weekly
review instead of the short daily check-in."""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

INFLUXDB_URL = os.environ["INFLUXDB_URL"]  # e.g. http://172.17.0.1:8187
# DailyStats/SleepSummary carry a per-device tag (watch, HRM strap, bike computer,
# speed sensor) — several fields (totalSteps, stressPercentage) are only meaningful
# from the watch itself and silently wrong from other devices (e.g. a chest strap
# reporting 227 steps vs. the watch's 1790 for the same day). Filtering on this
# pins those queries to the one full/reliable source.
WATCH_DEVICE = os.environ.get("WATCH_DEVICE", "fenix 8 - 47mm, AMOLED")
INFLUXDB_DB = os.environ.get("INFLUXDB_DB", "GarminStats")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
LANGUAGE = os.environ.get("LANGUAGE", "English")  # e.g. English, Nederlands, Deutsch
LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "Europe/Amsterdam"))
NOW_LOCAL = datetime.now(LOCAL_TZ)
IS_WEEKLY = NOW_LOCAL.weekday() == 6  # Sunday, in lokale tijd
STALE_AFTER_HOURS = 36
OUTPUT_FILE = "/app/output/advies.json"
# Persisted on the coach-home volume so it survives container restarts/rebuilds.
LOG_FILE = os.path.expanduser("~/coach_log.json")
LOG_HISTORY_DAYS = 14


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
        result.append(
            {
                "date": r["time"][:10],
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
    for sport, types in (
        ("running", ["running"]),
        ("cycling", ["road_biking", "indoor_cycling", "mountain_biking", "gravel_cycling", "cycling"]),
    ):
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
    # zones with the Karvonen formula instead of a real FTP/threshold test.
    resting_hr = (today[0].get("restingHeartRate") if today else None) or avg(last_28d, "restingHeartRate")
    # Only cycling activities for max HR — running typically gives 5-10 bpm higher
    # peaks and would skew the cycling zones.
    max_hr_rows = influx_query(
        "SELECT max(maxHR) FROM ActivitySummary WHERE time > now() - 90d "
        "AND (activityType = 'road_biking' OR activityType = 'indoor_cycling' OR activityType = 'mountain_biking')"
    )
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
    entries = read_coach_log()
    entries.append(
        {
            "date": NOW_LOCAL.date().isoformat(),
            "weekly": weekly,
            "advice": advice,
        }
    )
    with open(LOG_FILE, "w") as f:
        json.dump(entries, f, ensure_ascii=False)


JSON_SCHEMA_DAILY = """{
  "status": "<1 sentence: sleep/resting HR/body battery/training readiness score>",
  "run_tip": "<1-2 sentences: concrete workout type (endurance/tempo/interval/recovery), duration, and intensity target (HR range or pace) — not just a HR zone>",
  "bike_tip": "<1-2 sentences: concrete workout type (endurance/tempo/interval/recovery), duration, and intensity target (HR range) — not just a HR zone>",
  "color": "green or yellow"
}"""

JSON_SCHEMA_WEEKLY = """{
  "performance": "<2-3 sentences overall weekly assessment, referencing the trend across recent_activities_14d, coach_log, vo2max, and intensity_distribution_7d/28d>",
  "recovery": "<2-3 sentences, explicitly mention the ACWR ratio and training_load_by_sport>",
  "run_advice": "<2-3 sentences: concrete plan for the coming week (e.g. which days for intervals/tempo/long run vs. recovery), not just a single tip>",
  "bike_advice": "<2-3 sentences: concrete plan for the coming week (e.g. which days for intervals/tempo/long ride vs. recovery), not just a single tip>",
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
  (more than 36 hours old) — don't rely on it, fall back on sleep/resting HR/ACWR.
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
- cycling_hr_zones: NO measured threshold available (Garmin doesn't provide this for cycling on
  this account) — these are generic, calculated heart rate zones (Karvonen formula based on
  resting HR and measured max HR during cycling activities), less precise than the running
  threshold. Mention this when giving cycling advice.
- today.sleep_hours, today.sleep_score (Garmin's own 0-100 sleep quality score) and
  today.sleep_vs_prior_6d_avg_hours (difference vs. the average of the preceding 6 days, so
  not counting today itself): sleep is a FULL decision factor alongside readiness/ACWR, not
  just a status figure. A clear deficit (e.g. -1.5h or more) or a low sleep_score should
  noticeably temper the intensity/duration of the advice, even if body battery/readiness look
  fine on their own — a high body battery after little sleep mostly reflects current energy,
  not full physical recovery.
- today.activities_already_done_today: sport activities ALREADY completed since local midnight
  today (type, name, distance, duration in minutes, avg heart rate, calories). The user
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
- recent_activities_14d: full activity history of the last 14 days (date, type, duration,
  avg HR), NOT necessarily one entry per day — there can be, and often are, rest days with no
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
- baseline_deviation.hrv_deviation_sd / resting_hr_deviation_sd: today's HRV/resting HR
  expressed as standard deviations from your 28-day rolling baseline. Roughly: within ±1 SD
  is normal day-to-day variation, beyond ±1 SD is a notable deviation worth mentioning,
  beyond ±2 SD is a strong signal (HRV notably below baseline or RHR notably above baseline
  both suggest incomplete recovery — treat this as at least as important as Garmin's own
  training_readiness score, since it's a more transparent, directly-computed signal). If
  "available": false, this data isn't available yet (needs at least 7 days of history)."""

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
Respond with valid JSON per this schema. Write all text values in {LANGUAGE}, EXCEPT the
"color" field — always keep that in English exactly as shown in the schema (green/yellow/
orange/red), regardless of {LANGUAGE}:
{schema}

{disclaimer}

Write ONLY the JSON (nothing else, no explanation, no markdown code block) to the file
{OUTPUT_FILE} using the Write tool. Then give a brief confirmation in the chat."""


def call_claude(prompt: str) -> dict:
    """Sends the prompt to the permanent 'coach' tmux session instead of spawning
    a fresh `claude -p` subprocess — the latter triggers an expensive cache write
    (system prompt/tools) every time, which burns a disproportionate amount of
    the session quota on a cold/fresh container start.

    Claude writes the answer itself to OUTPUT_FILE (via the Write tool) instead
    of us trying to scrape it from the tmux screen text — screen-text parsing
    turned out to be fragile (race conditions with intermediate 'thinking'
    frames, line wraps that broke JSON strings, markers seen too early/late)."""
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
            "footer": {"text": "Garmin AI Coach — Claude"},
        }
    return {
        "title": f"📅 Today — {today_str}",
        "color": color,
        "fields": [
            {"name": "Status", "value": _field(advice.get("status")), "inline": False},
            {"name": "🏃 Running", "value": _field(advice.get("run_tip")), "inline": False},
            {"name": "🚴 Cycling", "value": _field(advice.get("bike_tip")), "inline": False},
        ],
        "footer": {"text": "Garmin AI Coach — Claude"},
    }


def post_error_to_discord(error: Exception):
    embed = {
        "title": "⚠️ Garmin AI Coach — failed",
        "color": 0x95A5A6,
        "description": f"```{str(error)[:1900]}```",
        "footer": {"text": "Garmin AI Coach — Claude"},
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
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        post_error_to_discord(e)
        sys.exit(1)
