#!/usr/bin/env python3
"""Shared sync engine used by both the historical backfill and the ongoing 5-minute
sync (dashboard.py's scheduler thread). All field mappings below were verified
against REAL API responses from a live Garmin account, not assumed from docs —
several genuinely differ from what garmin-grafana's InfluxDB schema (and this
project's own earlier assumptions) suggested: get_sleep_data()'s score lives under
dailySleepDTO.sleepScores.overall.value (not a flat sleepScore field),
get_training_readiness()/get_max_metrics() return LISTS (most-recent-first for
readiness), get_training_status() nests everything under a per-deviceId key, and
get_activity_splits()'s laps carry no per-lap sport field."""

import json
from datetime import datetime, timedelta, timezone

import db
import garmin_client

# Same activityType vocabulary coach.py's SPORT_TYPES already uses (verified
# against a real activity: {"typeKey": "road_biking", ...}) — kept in sync with
# that constant rather than re-deriving it, since both describe the same Garmin API.
CYCLING_TYPES = {"road_biking", "indoor_cycling", "mountain_biking", "gravel_cycling", "cycling"}

# Garmin purges intraday detail (body battery curves, HRV intraday) older than
# ~6 months — API calls for older dates return empty rather than erroring, so this
# is used to skip attempting-then-failing rather than to validate a hard error.
INTRADAY_CUTOFF_DAYS = 180


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sync_daily_summary(client, date: str) -> bool:
    stats = garmin_client.paced_call(client.get_stats, date)
    if not stats:
        return False
    db.upsert(
        "daily_summary",
        ["date"],
        {
            "date": date,
            "resting_hr": stats.get("restingHeartRate"),
            "steps": stats.get("totalSteps"),
            "stress_avg": stats.get("averageStressLevel"),
            "bb_high": stats.get("bodyBatteryHighestValue"),
            "bb_low": stats.get("bodyBatteryLowestValue"),
            "synced_at": _now_iso(),
            "raw_json": json.dumps(stats),
        },
    )
    return True


def _sync_sleep(client, date: str) -> bool:
    sleep = garmin_client.paced_call(client.get_sleep_data, date)
    dto = sleep.get("dailySleepDTO") if sleep else None
    if not dto or not dto.get("sleepTimeSeconds"):
        return False
    overall = (dto.get("sleepScores") or {}).get("overall") or {}
    db.upsert(
        "sleep",
        ["date"],
        {
            "date": date,
            "sleep_seconds": dto.get("sleepTimeSeconds"),
            "sleep_score": overall.get("value"),
            "deep_s": dto.get("deepSleepSeconds"),
            "light_s": dto.get("lightSleepSeconds"),
            "rem_s": dto.get("remSleepSeconds"),
            "awake_s": dto.get("awakeSleepSeconds"),
            "synced_at": _now_iso(),
            "raw_json": json.dumps(sleep),
        },
    )
    return True


def _sync_hrv(client, date: str) -> bool:
    hrv = garmin_client.paced_call(client.get_hrv_data, date)
    summary = hrv.get("hrvSummary") if hrv else None
    if not summary:
        return False
    baseline = summary.get("baseline") or {}
    db.upsert(
        "hrv",
        ["date"],
        {
            "date": date,
            "last_night_avg": summary.get("lastNightAvg"),
            "weekly_avg": summary.get("weeklyAvg"),
            "status": summary.get("status"),
            "baseline_low_upper": baseline.get("lowUpper"),
            "baseline_balanced_low": baseline.get("balancedLow"),
            "baseline_balanced_upper": baseline.get("balancedUpper"),
            "synced_at": _now_iso(),
            "raw_json": json.dumps(hrv),
        },
    )
    # hrvReadings carries the intraday time series for this night — only present
    # when Garmin still has intraday detail for this date (cold-storage cutoff).
    for reading in hrv.get("hrvReadings") or []:
        ts_str = reading.get("readingTimeGMT")
        if not ts_str or reading.get("hrvValue") is None:
            continue
        ts = int(datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp())
        db.upsert("hrv_intraday", ["ts"], {"ts": ts, "hrv_value": reading["hrvValue"]})
    return True


def _sync_training_readiness(client, date: str) -> bool:
    # Returns a list of readings through the day, most-recent-first (verified) —
    # the first entry is the day's current/final readiness snapshot.
    readings = garmin_client.paced_call(client.get_training_readiness, date)
    if not readings:
        return False
    r = readings[0]
    db.upsert(
        "training_readiness",
        ["date"],
        {
            "date": date,
            "score": r.get("score"),
            "level": r.get("level"),
            "acute_load": r.get("acuteLoad"),
            "recovery_time_min": r.get("recoveryTime"),
            "sleep_score": r.get("sleepScore"),
            "factor_hrv": r.get("hrvFactorPercent"),
            "factor_sleep": r.get("sleepScoreFactorPercent"),
            "factor_recovery_time": r.get("recoveryTimeFactorPercent"),
            "factor_acwr": r.get("acwrFactorPercent"),
            "factor_stress_history": r.get("stressHistoryFactorPercent"),
            "synced_at": _now_iso(),
            "raw_json": json.dumps(readings),
        },
    )
    return True


def _sync_training_status(client, date: str) -> bool:
    ts = garmin_client.paced_call(client.get_training_status, date)
    # latestTrainingStatusData is nested under mostRecentTrainingStatus, NOT at the
    # top level — confirmed by cross-referencing garmin-grafana's own source
    # (garmin_fetch.py's get_training_status) after this parity check found it
    # consistently empty; reading it from the top level (an earlier assumption made
    # without checking a working reference implementation) silently returned {}
    # every time despite the underlying data genuinely being present.
    mrts = (ts or {}).get("mostRecentTrainingStatus") or {}
    latest_map = mrts.get("latestTrainingStatusData") or {}
    if not latest_map:
        return False
    # Keyed by deviceId — take the entry marked primaryTrainingDevice if present,
    # else just the first (single-device accounts, the common case, have only one).
    entry = next((v for v in latest_map.values() if v.get("primaryTrainingDevice")), None)
    entry = entry or next(iter(latest_map.values()))
    acute = entry.get("acuteTrainingLoadDTO") or {}
    db.upsert(
        "training_status",
        ["date"],
        {
            "date": date,
            "status_phrase": entry.get("trainingStatusFeedbackPhrase"),
            "acwr": acute.get("dailyAcuteChronicWorkloadRatio"),
            "acute_load": acute.get("dailyTrainingLoadAcute"),
            "chronic_load": acute.get("dailyTrainingLoadChronic"),
            "synced_at": _now_iso(),
            "raw_json": json.dumps(ts),
        },
    )
    return True


def _sync_max_metrics(client, date: str) -> bool:
    metrics = garmin_client.paced_call(client.get_max_metrics, date)
    if not metrics:
        return False
    m = metrics[0]
    generic = m.get("generic") or {}
    cycling = m.get("cycling") or {}
    if generic.get("vo2MaxValue") is None and cycling.get("vo2MaxValue") is None:
        return False
    db.upsert(
        "max_metrics",
        ["date"],
        {
            "date": date,
            "vo2max_run": generic.get("vo2MaxValue"),
            "vo2max_cycle": cycling.get("vo2MaxValue"),
            "synced_at": _now_iso(),
            "raw_json": json.dumps(metrics),
        },
    )
    return True


def _sync_activities(client, date: str) -> int:
    """Returns the number of activities synced for this date. Fetches laps for
    each activity too (needed for _cycling_power_zones()-equivalent FTP estimation
    later) — paced individually since this is N+1 API calls per day with activities."""
    activities = garmin_client.paced_call(client.get_activities_by_date, date, date)
    count = 0
    for a in activities or []:
        activity_id = a.get("activityId")
        if activity_id is None:
            continue
        activity_type = (a.get("activityType") or {}).get("typeKey")
        start_local = a.get("startTimeLocal")
        db.upsert(
            "activities",
            ["activity_id"],
            {
                "activity_id": activity_id,
                "date": (start_local or date)[:10],
                "start_utc": a.get("startTimeGMT"),
                "start_local": start_local,
                "type": activity_type,
                "name": a.get("activityName"),
                "duration_s": round(a["elapsedDuration"]) if a.get("elapsedDuration") else None,
                "distance_m": a.get("distance"),
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "calories": a.get("calories"),
                "training_load": a.get("activityTrainingLoad"),
                "te_aerobic": a.get("aerobicTrainingEffect"),
                "te_anaerobic": a.get("anaerobicTrainingEffect"),
                "hr_zone1_s": a.get("hrTimeInZone_1"),
                "hr_zone2_s": a.get("hrTimeInZone_2"),
                "hr_zone3_s": a.get("hrTimeInZone_3"),
                "hr_zone4_s": a.get("hrTimeInZone_4"),
                "hr_zone5_s": a.get("hrTimeInZone_5"),
                "avg_power": a.get("avgPower"),
                "vo2max": a.get("vO2MaxValue"),
                "synced_at": _now_iso(),
                "raw_json": json.dumps(a),
            },
        )
        if activity_type in CYCLING_TYPES:
            _sync_activity_laps(client, activity_id)
        count += 1
    return count


def _sync_activity_laps(client, activity_id: int):
    try:
        splits = garmin_client.paced_call(client.get_activity_splits, activity_id)
    except Exception:
        return
    for idx, lap in enumerate(splits.get("lapDTOs") or []):
        db.upsert(
            "activity_laps",
            ["activity_id", "lap_idx"],
            {
                "activity_id": activity_id,
                "lap_idx": idx,
                "duration_s": round(lap["duration"]) if lap.get("duration") else None,
                "avg_power": lap.get("averagePower"),
                "avg_hr": lap.get("averageHR"),
            },
        )


def _sync_ftp(client, date: str) -> bool:
    ftp = garmin_client.paced_call(client.get_cycling_ftp)
    # get_cycling_ftp()'s return type is whatever the endpoint happens to send
    # (dict when data exists, per a live test — defensively handle a list/empty
    # response too rather than assuming dict always).
    if isinstance(ftp, list):
        ftp = ftp[0] if ftp else None
    if not ftp or not ftp.get("functionalThresholdPower"):
        return False
    db.upsert(
        "ftp",
        ["date"],
        {
            "date": date,
            "garmin_ftp_watts": ftp.get("functionalThresholdPower"),
            "synced_at": _now_iso(),
            "raw_json": json.dumps(ftp),
        },
    )
    return True


def _sync_lactate_threshold(client, date: str) -> bool:
    lt = garmin_client.paced_call(client.get_lactate_threshold, latest=True)
    speed_hr = (lt or {}).get("speed_and_heart_rate") or {}
    if not speed_hr.get("heartRate"):
        return False
    # speed_hr["speed"] is ALREADY seconds-per-meter (pace), not m/s speed —
    # verified against a real response: 0.34166571 -> 5:42 min/km directly
    # (matching the value later confirmed against a known-good comparison run),
    # whereas treating it as m/s and inverting gives an absurd ~48:47 min/km.
    # Same field semantics as the old InfluxDB SpeedThreshold_RUNNING coach.py
    # already relied on — no inversion needed, just store it as-is.
    db.upsert(
        "lactate_threshold",
        ["date"],
        {
            "date": date,
            "hr_threshold_running": speed_hr.get("heartRate"),
            "speed_threshold_sec_per_m": speed_hr.get("speed"),
            "synced_at": _now_iso(),
            "raw_json": json.dumps(lt),
        },
    )
    return True


def _sync_body_battery_intraday(client, date: str):
    bb = garmin_client.paced_call(client.get_body_battery, date, date)
    for day in bb or []:
        for ts_ms, level in day.get("bodyBatteryValuesArray") or []:
            if level is None:
                continue
            db.upsert("bb_intraday", ["ts"], {"ts": ts_ms // 1000, "level": level})


def sync_day(client, date: str, intraday: bool = True) -> dict:
    """Fetches and upserts every per-day table for one date. Returns a small
    per-datatype result dict for progress reporting during backfill."""
    result = {}
    result["daily_summary"] = _sync_daily_summary(client, date)
    result["sleep"] = _sync_sleep(client, date)
    result["hrv"] = _sync_hrv(client, date)
    result["training_readiness"] = _sync_training_readiness(client, date)
    result["training_status"] = _sync_training_status(client, date)
    result["max_metrics"] = _sync_max_metrics(client, date)
    result["ftp"] = _sync_ftp(client, date)
    result["lactate_threshold"] = _sync_lactate_threshold(client, date)
    result["activities"] = _sync_activities(client, date)

    is_recent = (datetime.now(timezone.utc).date() - datetime.fromisoformat(date).date()).days <= INTRADAY_CUTOFF_DAYS
    if intraday and is_recent:
        _sync_body_battery_intraday(client, date)
        result["intraday"] = True
    else:
        result["intraday"] = False
    return result


def suggest_backfill_start_date(client) -> str | None:
    """Cheap (~2 requests) suggestion for the first-run backfill date-picker
    default — no official "earliest data" endpoint exists, so the oldest logged
    activity's date is used as the practical proxy. Returns an ISO date string,
    or None if the account has no activities at all."""
    count = garmin_client.paced_call(client.count_activities)
    if not count:
        return None
    oldest = garmin_client.paced_call(client.get_activities, count - 1, 1)
    if not oldest:
        return None
    start_local = oldest[0].get("startTimeLocal")
    return start_local[:10] if start_local else None


BACKFILL_PROGRESS_KEY = "backfill_progress"


def run_backfill(start_date: str, end_date: str, progress_cb=None):
    """Iterates date-by-date newest -> oldest (resumable, matches garmin-grafana's
    battle-tested pacing), checkpointing after every committed day so a restart
    resumes from where it left off rather than re-fetching already-synced days."""
    client = garmin_client.get_client()
    start = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date()
    all_dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    all_dates.reverse()  # newest -> oldest

    checkpoint = db.get_sync_state(BACKFILL_PROGRESS_KEY, default={})
    resume_from = checkpoint.get("cursor_date") if checkpoint.get("end_date") == end_date else None
    remaining = all_dates
    if resume_from and resume_from in all_dates:
        remaining = all_dates[all_dates.index(resume_from):]

    total = len(all_dates)
    done = total - len(remaining)

    for date in remaining:
        sync_day(client, date, intraday=True)
        done += 1
        db.set_sync_state(
            BACKFILL_PROGRESS_KEY,
            {"cursor_date": date, "done": done, "total": total, "end_date": end_date, "running": done < total},
        )
        if progress_cb:
            progress_cb(date, done, total)

    db.set_sync_state(BACKFILL_PROGRESS_KEY, {"cursor_date": None, "done": total, "total": total, "end_date": end_date, "running": False})
