#!/usr/bin/env python3
"""Garmin AI coach: queries InfluxDB, aggregates metrics, asks Claude Code for
advice, posts the result to Discord. Runs daily; on Sundays does a full weekly
review instead of the short daily check-in."""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

INFLUXDB_URL = os.environ["INFLUXDB_URL"]  # e.g. http://172.17.0.1:8187
INFLUXDB_DB = os.environ.get("INFLUXDB_DB", "GarminStats")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
LANGUAGE = os.environ.get("LANGUAGE", "English")  # e.g. English, Nederlands, Deutsch
LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "Europe/Amsterdam"))
NOW_LOCAL = datetime.now(LOCAL_TZ)
IS_WEEKLY = NOW_LOCAL.weekday() == 6  # Sunday, in lokale tijd
STALE_AFTER_HOURS = 36
OUTPUT_FILE = "/app/output/advies.json"


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
        f'bodyBatteryHighestValue, bodyBatteryLowestValue, sleepingSeconds '
        f'FROM "DailyStats" WHERE time >= \'{start.isoformat()}\' AND time < \'{end.isoformat()}\' '
        f'ORDER BY time DESC'
    )
    return influx_query(q)


def build_metrics() -> dict:
    today = daily_stats_window(0, 1)
    last_7d = daily_stats_window(0, 7)
    prev_7d = daily_stats_window(7, 7)
    last_28d = daily_stats_window(0, 28)

    today_sleep_h = round(today[0]["sleepingSeconds"] / 3600, 1) if today and today[0].get("sleepingSeconds") else None
    # sleep_vs_7d_avg compares against the 6 days BEFORE today, not including today
    # itself — otherwise the deviation gets structurally dampened by including
    # today in its own baseline average.
    prior_6d = daily_stats_window(1, 6)
    prior_6d_sleep_avg = avg(prior_6d, "sleepingSeconds")

    return {
        "today": {
            "resting_hr": today[0].get("restingHeartRate") if today else None,
            "steps": today[0].get("totalSteps") if today else None,
            "body_battery_high": today[0].get("bodyBatteryHighestValue") if today else None,
            "body_battery_low": today[0].get("bodyBatteryLowestValue") if today else None,
            "sleep_hours": today_sleep_h,
            "sleep_vs_prior_6d_avg_hours": round(today_sleep_h - prior_6d_sleep_avg / 3600, 1)
            if today_sleep_h is not None and prior_6d_sleep_avg
            else None,
            "activities_already_done_today": _activities_today(),
        },
        "last_7d_avg": {
            "resting_hr": avg(last_7d, "restingHeartRate"),
            "steps": avg(last_7d, "totalSteps"),
            "stress_pct": avg(last_7d, "stressPercentage"),
            "sleep_hours": round(avg(last_7d, "sleepingSeconds") / 3600, 1)
            if avg(last_7d, "sleepingSeconds")
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
    }


def _activities_today() -> list[dict]:
    start = local_midnight_utc(0)
    rows = influx_query(
        f'SELECT activityType, activityName, distance, elapsedDuration, averageHR, calories '
        f'FROM "ActivitySummary" WHERE time >= \'{start.isoformat()}\''
    )
    result = []
    seen = set()
    for r in rows:
        activity_type = r.get("activityType")
        if not activity_type or activity_type == "No Activity":
            continue
        key = (activity_type, r.get("activityName"), r.get("distance"))
        if key in seen:  # garmin-fetch-data can write an activity twice on re-sync
            continue
        seen.add(key)
        result.append(
            {
                "type": activity_type,
                "name": r.get("activityName"),
                "distance_km": round(r["distance"] / 1000, 1) if r.get("distance") else None,
                "duration_min": round(r["elapsedDuration"] / 60) if r.get("elapsedDuration") else None,
                "avg_hr": r.get("averageHR"),
                "calories": r.get("calories"),
            }
        )
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


JSON_SCHEMA_DAILY = """{
  "status": "<1 sentence: sleep/resting HR/body battery/training readiness score>",
  "run_tip": "<1-2 sentences running advice>",
  "bike_tip": "<1-2 sentences cycling advice>",
  "color": "green or yellow"
}"""

JSON_SCHEMA_WEEKLY = """{
  "performance": "<2-3 sentences overall weekly assessment>",
  "recovery": "<2-3 sentences, explicitly mention the ACWR ratio>",
  "run_advice": "<2-3 sentences running advice for the coming week>",
  "bike_advice": "<2-3 sentences cycling advice for the coming week>",
  "watch_point": "<1-2 sentences, or 'Nothing notable'>",
  "color": "green, orange or red"
}"""


def build_prompt(metrics: dict, weekly: bool) -> str:
    m = json.dumps(metrics, indent=2, ensure_ascii=False)
    disclaimer = (
        "No medical claims or diagnoses. For persistent issues, refer to a doctor "
        "or physiotherapist — avoid overly confident statements."
    )
    context = """Field explanations:
- training_readiness.score (0-100) and .level: Garmin's own readiness score for today.
  If "available": false or "data_stale": true is set: this data is missing or stale
  (more than 36 hours old) — don't rely on it, fall back on sleep/resting HR/ACWR.
- training_status.acute_chronic_load_ratio (ACWR): <0.8 = too little load (undertrained),
  0.8-1.3 = healthy zone, 1.3-1.5 = caution (rising risk), >1.5 = elevated injury risk
  (ramped up too fast). This covers overall load (running + cycling combined).
- training_status.status: Garmin's classification (e.g. PRODUCTIVE, PEAKING, OVERREACHING, RECOVERY).
- lactate_threshold_running.threshold_hr_running: exact, measured heart rate threshold for running.
- lactate_threshold_running.threshold_pace_min_per_km: exact, measured threshold pace for running,
  ALREADY FORMATTED as "M:SS min/km" (e.g. "5:41 min/km") — reuse this notation verbatim,
  don't recalculate or interpret it as a decimal number.
- cycling_hr_zones: NO measured threshold available (Garmin doesn't provide this for cycling on
  this account) — these are generic, calculated heart rate zones (Karvonen formula based on
  resting HR and measured max HR during cycling activities), less precise than the running
  threshold. Mention this when giving cycling advice.
- today.sleep_hours and today.sleep_vs_prior_6d_avg_hours (difference vs. the average of the
  preceding 6 days, so not counting today itself): sleep is a FULL decision factor alongside
  readiness/ACWR, not just a status figure. A clear deficit (e.g. -1.5h or more) should
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
        'concrete, actionable training advice for TODAY — not just "take it easy" but a '
        'specific workout suggestion with type, duration, and intensity target.'
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


def main():
    metrics = build_metrics()
    prompt = build_prompt(metrics, IS_WEEKLY)
    advice = call_claude(prompt)
    embed = build_embed(advice, IS_WEEKLY)
    post_discord(embed)
    print(f"Done ({'weekly' if IS_WEEKLY else 'daily'}):", json.dumps(advice, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        post_error_to_discord(e)
        sys.exit(1)
