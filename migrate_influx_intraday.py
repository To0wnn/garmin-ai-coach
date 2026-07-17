#!/usr/bin/env python3
"""Stage 4 (one-off, run-once): migrates BodyBatteryIntraday/HRV_Intraday history
from the still-running InfluxDB/garmin-grafana pipeline into SQLite's bb_intraday/
hrv_intraday tables, BEFORE garmin-grafana is ever decommissioned.

This is the one piece of data this migration cannot treat as "the new pipeline will
catch up eventually" — Garmin's API purges intraday detail older than ~6 months
(confirmed platform behavior, not a bug to work around), so anything InfluxDB has
that's already older than that window can never be re-fetched again. Pull the WHOLE
history InfluxDB has, not just the last 6 months — that's the entire point.

Not wired into the app/cron/dashboard — a standalone script, run manually once (with
garmin-grafana/InfluxDB still up and reachable), supervised. Reuses coach.py's own
urllib-based query approach (no new dependency), and db.py's upsert() for idempotent,
safe-to-rerun writes if it's interrupted partway.

Usage: python3 migrate_influx_intraday.py [--dry-run]
Requires INFLUXDB_URL / INFLUXDB_DB env vars (same as coach.py)."""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import db

INFLUXDB_URL = os.environ["INFLUXDB_URL"]
INFLUXDB_DB = os.environ.get("INFLUXDB_DB", "GarminStats")
WATCH_DEVICE = os.environ.get("WATCH_DEVICE", "fenix 8 - 47mm, AMOLED")
CHUNK_DAYS = 30  # month-sized chunks — bounded response size, not one giant query


def influx_query(q: str) -> list[dict]:
    params = urllib.parse.urlencode({"db": INFLUXDB_DB, "q": q})
    with urllib.request.urlopen(f"{INFLUXDB_URL}/query?{params}", timeout=60) as resp:
        data = json.load(resp)
    result = data.get("results", [{}])[0]
    if "error" in result:
        raise RuntimeError(f"InfluxDB query error: {result['error']} (query: {q})")
    series = result.get("series")
    if not series:
        return []
    cols = series[0]["columns"]
    return [dict(zip(cols, row)) for row in series[0]["values"]]


def _earliest_time(measurement: str) -> datetime | None:
    rows = influx_query(f'SELECT * FROM "{measurement}" ORDER BY time ASC LIMIT 1')
    if not rows:
        return None
    return datetime.fromisoformat(rows[0]["time"].replace("Z", "+00:00"))


def _migrate_measurement(measurement: str, value_field: str, table: str, db_col: str, dry_run: bool) -> int:
    earliest = _earliest_time(measurement)
    if earliest is None:
        print(f"{measurement}: no data in InfluxDB, skipping.")
        return 0

    now = datetime.now(timezone.utc)
    total_rows = 0
    chunk_start = earliest.replace(hour=0, minute=0, second=0, microsecond=0)

    while chunk_start < now:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), now)
        q = (
            f'SELECT {value_field} FROM "{measurement}" '
            f"WHERE time >= '{chunk_start.isoformat()}' AND time < '{chunk_end.isoformat()}' "
            f"AND \"Device\" = '{WATCH_DEVICE}'"
        )
        rows = influx_query(q)
        month_count = 0
        for r in rows:
            val = r.get(value_field)
            if val is None:
                continue
            ts = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
            ts_epoch = int(ts.timestamp())
            if not dry_run:
                db.upsert(table, ["ts"], {"ts": ts_epoch, db_col: val})
            month_count += 1
        total_rows += month_count
        print(
            f"{measurement}: {chunk_start.date()} to {chunk_end.date()} "
            f"— {month_count} rows{' (dry run)' if dry_run else ''}"
        )
        chunk_start = chunk_end

    return total_rows


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN — no writes to SQLite will be made.\n")

    db.init_schema()

    print(f"=== Migrating BodyBatteryIntraday (Device={WATCH_DEVICE!r}) ===")
    bb_count = _migrate_measurement("BodyBatteryIntraday", "BodyBatteryLevel", "bb_intraday", "level", dry_run)

    print(f"\n=== Migrating HRV_Intraday (Device={WATCH_DEVICE!r}) ===")
    hrv_count = _migrate_measurement("HRV_Intraday", "hrvValue", "hrv_intraday", "hrv_value", dry_run)

    print(f"\n=== Done ===")
    print(f"BodyBatteryIntraday: {bb_count} rows migrated")
    print(f"HRV_Intraday: {hrv_count} rows migrated")

    if not dry_run:
        db.set_sync_state(
            "influx_migration_completed_at",
            datetime.now(timezone.utc).isoformat(),
        )
        print("\nRecorded influx_migration_completed_at in sync_state — this is the")
        print("go/no-go marker before garmin-grafana can be decommissioned (Stage 7).")


if __name__ == "__main__":
    main()
