#!/usr/bin/env python3
"""Stage 3 (parity verification) — a TEMPORARY, standalone parallel implementation
of coach.py's build_metrics(), sourced from SQLite instead of InfluxDB. Not wired
into coach.py or dashboard.py; used only by compare_parity.py to diff its output
against the real, InfluxDB-backed build_metrics() for several real days before any
production behavior changes (per the migration plan's staged approach). Deleted once
Stage 6 makes this the real implementation — this file's existence itself is the
"temporary parallel function" the plan calls for, kept separate from coach.py rather
than a flag/branch inside it so coach.py's actual behavior is provably untouched
during this verification phase.

Every metric here reuses coach.py's own field-mapping decisions (recovery-window
lengths, rolling-baseline math, FTP-from-best-lap heuristic) — only the query layer
underneath changes from InfluxQL to SQL. Dates are compared as local-calendar-date
TEXT, matching db.py's schema, rather than coach.py's UTC-midnight-window arithmetic."""

import json
from datetime import datetime, timedelta

import coach  # reuses NOW_LOCAL, LOCAL_TZ, SPORT_TYPES, _stddev, _format_pace — no duplication of pure logic
import db

LOG_HISTORY_DAYS = coach.LOG_HISTORY_DAYS


def _date(days_ago: int = 0) -> str:
    return (coach.NOW_LOCAL.date() - timedelta(days=days_ago)).isoformat()


def _date_range(start_days_ago: int, end_days_ago: int = 0) -> tuple[str, str]:
    """Inclusive [start, end] range as ISO date strings, oldest first."""
    return _date(start_days_ago), _date(end_days_ago)


def avg(rows: list[dict], field: str) -> float | None:
    vals = [r[field] for r in rows if r.get(field) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _daily_stats_window(days_back: int, window_days: int) -> list[dict]:
    start, end = _date_range(days_back + window_days - 1, days_back)
    return db.query(
        "SELECT date, resting_hr as restingHeartRate, steps as totalSteps, "
        "stress_avg as stressPercentage, bb_high as bodyBatteryHighestValue, "
        "bb_low as bodyBatteryLowestValue FROM daily_summary "
        "WHERE date BETWEEN ? AND ? ORDER BY date DESC",
        (start, end),
    )


def _sleep_window(days_back: int, window_days: int) -> list[dict]:
    start, end = _date_range(days_back + window_days - 1, days_back)
    return db.query(
        "SELECT date, sleep_seconds as sleepTimeSeconds, sleep_score as sleepScore "
        "FROM sleep WHERE date BETWEEN ? AND ? ORDER BY date DESC",
        (start, end),
    )


def _body_battery_current() -> dict:
    rows = db.query("SELECT ts, level FROM bb_intraday ORDER BY ts DESC LIMIT 1")
    if not rows:
        return {"available": False}
    ts = datetime.fromtimestamp(rows[0]["ts"], tz=coach.LOCAL_TZ)
    age_minutes = (datetime.now(coach.LOCAL_TZ) - ts).total_seconds() / 60
    return {"available": True, "level": rows[0]["level"], "age_minutes": round(age_minutes)}


def _activities_since(start_date: str) -> list[dict]:
    rows = db.query(
        "SELECT * FROM activities WHERE date >= ? ORDER BY start_utc DESC", (start_date,)
    )
    result = []
    for r in rows:
        start_local = r.get("start_local")
        local_time = start_local[11:16] if start_local and len(start_local) >= 16 else None
        result.append(
            {
                "date": r["date"],
                "local_time": local_time,
                "type": r.get("type"),
                "name": r.get("name"),
                "distance_km": round(r["distance_m"] / 1000, 1) if r.get("distance_m") else None,
                "duration_min": round(r["duration_s"] / 60) if r.get("duration_s") else None,
                "avg_hr": r.get("avg_hr"),
                "calories": r.get("calories"),
            }
        )
    return result


def _intensity_distribution(days: int) -> dict:
    start = _date(days - 1)
    rows = db.query(
        "SELECT hr_zone1_s, hr_zone2_s, hr_zone3_s, hr_zone4_s, hr_zone5_s "
        "FROM activities WHERE date >= ?",
        (start,),
    )
    zones = [0.0] * 5
    keys = ["hr_zone1_s", "hr_zone2_s", "hr_zone3_s", "hr_zone4_s", "hr_zone5_s"]
    for r in rows:
        for i, k in enumerate(keys):
            zones[i] += r.get(k) or 0
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
    start_7d = _date(6)
    start_28d = _date(27)
    result = {}
    for sport, types in coach.SPORT_TYPES.items():
        placeholders = ",".join("?" for _ in types)
        rows_7d = db.query(
            f"SELECT training_load FROM activities WHERE date >= ? AND type IN ({placeholders})",
            (start_7d, *types),
        )
        rows_28d = db.query(
            f"SELECT training_load FROM activities WHERE date >= ? AND type IN ({placeholders})",
            (start_28d, *types),
        )
        load_7d = sum(r.get("training_load") or 0 for r in rows_7d)
        load_28d = sum(r.get("training_load") or 0 for r in rows_28d)
        weekly_avg_28d = load_28d / 4
        result[sport] = {
            "load_last_7d": round(load_7d),
            "weekly_avg_last_28d": round(weekly_avg_28d),
            "load_ramp_ratio": round(load_7d / weekly_avg_28d, 2) if weekly_avg_28d else None,
        }
    return result


def _vo2max_trend() -> dict:
    def latest_nonnull(col: str, before_days: int = 0) -> float | None:
        if before_days:
            rows = db.query(
                f"SELECT {col} FROM max_metrics WHERE date < ? AND {col} IS NOT NULL "
                f"ORDER BY date DESC LIMIT 1",
                (_date(before_days),),
            )
        else:
            rows = db.query(
                f"SELECT {col} FROM max_metrics WHERE {col} IS NOT NULL ORDER BY date DESC LIMIT 1"
            )
        return rows[0][col] if rows else None

    running_now = latest_nonnull("vo2max_run")
    running_28d_ago = latest_nonnull("vo2max_run", before_days=28)
    cycling_now = latest_nonnull("vo2max_cycle")
    cycling_28d_ago = latest_nonnull("vo2max_cycle", before_days=28)

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


def _baseline_deviation() -> dict:
    today_hrv_rows = db.query("SELECT last_night_avg FROM hrv WHERE date = ?", (_date(0),))
    today_hrv = today_hrv_rows[0]["last_night_avg"] if today_hrv_rows else None

    start, end = _date_range(28, 1)
    hrv_rows = db.query(
        "SELECT last_night_avg FROM hrv WHERE date BETWEEN ? AND ? AND last_night_avg IS NOT NULL",
        (start, end),
    )
    hrv_history = [r["last_night_avg"] for r in hrv_rows]

    rhr_start, rhr_end = _date_range(28, 1)
    rhr_rows = db.query(
        "SELECT resting_hr FROM daily_summary WHERE date BETWEEN ? AND ? AND resting_hr IS NOT NULL",
        (rhr_start, rhr_end),
    )
    rhr_history = [r["resting_hr"] for r in rhr_rows]
    today_rhr_rows = db.query("SELECT resting_hr FROM daily_summary WHERE date = ?", (_date(0),))
    today_rhr = today_rhr_rows[0]["resting_hr"] if today_rhr_rows else None

    result = {}
    if today_hrv is not None and len(hrv_history) >= 7:
        hrv_mean = sum(hrv_history) / len(hrv_history)
        hrv_sd = coach._stddev(hrv_history)
        result["hrv_today"] = round(today_hrv, 1)
        result["hrv_28d_baseline_mean"] = round(hrv_mean, 1)
        result["hrv_deviation_sd"] = round((today_hrv - hrv_mean) / hrv_sd, 1) if hrv_sd else None

    if today_rhr is not None and len(rhr_history) >= 7:
        rhr_mean = sum(rhr_history) / len(rhr_history)
        rhr_sd = coach._stddev(rhr_history)
        result["resting_hr_today"] = today_rhr
        result["resting_hr_28d_baseline_mean"] = round(rhr_mean, 1)
        result["resting_hr_deviation_sd"] = round((today_rhr - rhr_mean) / rhr_sd, 1) if rhr_sd else None

    return result or {"available": False}


STALE_AFTER_DAYS = 2  # day-granularity equivalent of coach.py's 36-hour STALE_AFTER_HOURS


def _latest_row(table: str) -> dict:
    rows = db.query(f"SELECT * FROM {table} ORDER BY date DESC LIMIT 1")
    if not rows:
        return {"available": False}
    row = rows[0]
    age_days = (coach.NOW_LOCAL.date() - datetime.fromisoformat(row["date"]).date()).days
    row["_stale"] = age_days > STALE_AFTER_DAYS
    return row


def _training_readiness() -> dict:
    r = _latest_row("training_readiness")
    if not r.get("available", True):
        return r
    return {
        "score": r.get("score"),
        "level": r.get("level"),
        "acute_load": r.get("acute_load"),
        "recovery_time_hours": round(r["recovery_time_min"] / 60, 1) if r.get("recovery_time_min") else None,
        "sleep_score": r.get("sleep_score"),
        "factor_hrv": r.get("factor_hrv"),
        "factor_sleep": r.get("factor_sleep"),
        "factor_recovery_time": r.get("factor_recovery_time"),
        "factor_acwr": r.get("factor_acwr"),
        "factor_stress_history": r.get("factor_stress_history"),
        "data_stale": r.get("_stale", False),
    }


def _training_status() -> dict:
    r = _latest_row("training_status")
    if not r.get("available", True):
        return r
    return {
        "status": r.get("status_phrase"),
        "acute_chronic_load_ratio": r.get("acwr"),
        "acute_load": r.get("acute_load"),
        "chronic_load": r.get("chronic_load"),
        "data_stale": r.get("_stale", False),
    }


def _lactate_threshold() -> dict:
    r = _latest_row("lactate_threshold")
    if not r.get("available", True):
        return r
    sec_per_m = r.get("speed_threshold_sec_per_m")
    pace_decimal = sec_per_m * 1000 / 60 if sec_per_m else None
    return {
        "threshold_hr_running": r.get("hr_threshold_running"),
        "threshold_pace_min_per_km": f"{coach._format_pace(pace_decimal)} min/km" if pace_decimal else None,
        "data_stale": r.get("_stale", False),
    }


def _cycling_hr_zones(today_rows: list[dict], last_28d: list[dict]) -> dict:
    resting_hr = (today_rows[0].get("restingHeartRate") if today_rows else None) or avg(last_28d, "restingHeartRate")
    placeholders = ",".join("?" for _ in coach.SPORT_TYPES["cycling"])
    max_hr_rows = db.query(
        f"SELECT MAX(max_hr) as max_hr FROM activities WHERE date >= ? AND type IN ({placeholders})",
        (_date(89), *coach.SPORT_TYPES["cycling"]),
    )
    max_hr = max_hr_rows[0]["max_hr"] if max_hr_rows else None
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


def _cycling_power_zones() -> dict:
    rows = db.query(
        "SELECT al.avg_power, al.duration_s FROM activity_laps al "
        "JOIN activities a ON a.activity_id = al.activity_id "
        "WHERE a.date >= ? AND a.type IN (" + ",".join("?" for _ in coach.SPORT_TYPES["cycling"]) + ")",
        (_date(89), *coach.SPORT_TYPES["cycling"]),
    )
    candidates = [
        r["avg_power"]
        for r in rows
        if r.get("avg_power") is not None and r.get("duration_s") is not None and r["duration_s"] >= coach.FTP_MIN_LAP_SECONDS
    ]
    if not candidates:
        return {"available": False}
    best_power = max(candidates)
    ftp = round(best_power * coach.FTP_FROM_20MIN_FACTOR)
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


def build_metrics() -> dict:
    today = _daily_stats_window(0, 1)
    last_7d = _daily_stats_window(0, 7)
    prev_7d = _daily_stats_window(7, 7)
    last_28d = _daily_stats_window(0, 28)

    today_sleep = _sleep_window(0, 1)
    today_sleep_h = round(today_sleep[0]["sleepTimeSeconds"] / 3600, 1) if today_sleep and today_sleep[0].get("sleepTimeSeconds") else None
    today_sleep_score = today_sleep[0].get("sleepScore") if today_sleep else None
    prior_6d_sleep = _sleep_window(1, 6)
    prior_6d_sleep_avg = avg(prior_6d_sleep, "sleepTimeSeconds")
    last_7d_sleep = _sleep_window(0, 7)

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
            "activities_already_done_today": _activities_since(_date(0)),
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
        "recent_activities_14d": _activities_since(_date(LOG_HISTORY_DAYS - 1)),
        "intensity_distribution_7d": _intensity_distribution(7),
        "intensity_distribution_28d": _intensity_distribution(28),
        "training_load_by_sport": _training_load_by_sport(),
        "vo2max": _vo2max_trend(),
    }


if __name__ == "__main__":
    print(json.dumps(build_metrics(), indent=2, ensure_ascii=False))
