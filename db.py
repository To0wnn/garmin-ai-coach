#!/usr/bin/env python3
"""SQLite datastore replacing InfluxDB/garmin-grafana — own Garmin data, own storage.
Dates are stored as local-calendar-date TEXT (YYYY-MM-DD) primary keys for every daily
table, so window queries are plain lexicographic string ranges (no UTC-midnight
conversion dance like the old InfluxDB local_midnight_utc() needed). Only the two true
intraday tables keep real UTC unix-epoch timestamps, since they're sub-day.

Not wired into coach.py yet — built and verified standalone first (Stage 1 of the
InfluxDB->SQLite migration plan), coach.py keeps reading InfluxDB exclusively until
parity is proven (see project plan)."""

import json
import os
import sqlite3

DB_FILE = os.path.expanduser("~/coach.db")

# raw_json on every per-day/per-activity table uniformly (schema-drift insurance —
# doubly important since cold-storage-purged intraday history can never be re-fetched
# if a column mapping turns out wrong the first time).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    resting_hr INTEGER,
    steps INTEGER,
    stress_avg REAL,
    bb_high INTEGER,
    bb_low INTEGER,
    synced_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS sleep (
    date TEXT PRIMARY KEY,
    sleep_seconds INTEGER,
    sleep_score INTEGER,
    deep_s INTEGER,
    light_s INTEGER,
    rem_s INTEGER,
    awake_s INTEGER,
    synced_at TEXT,
    raw_json TEXT
);

-- Column names follow get_hrv_data()'s real hrvSummary.baseline shape (verified
-- against a live response): balancedLow/balancedUpper is the BALANCED band Garmin
-- itself uses for the "BALANCED" status; lowUpper is a separate, lower cutoff.
CREATE TABLE IF NOT EXISTS hrv (
    date TEXT PRIMARY KEY,
    last_night_avg INTEGER,
    weekly_avg INTEGER,
    status TEXT,
    baseline_low_upper INTEGER,
    baseline_balanced_low INTEGER,
    baseline_balanced_upper INTEGER,
    synced_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS training_readiness (
    date TEXT PRIMARY KEY,
    score INTEGER,
    level TEXT,
    acute_load INTEGER,
    recovery_time_min INTEGER,
    sleep_score INTEGER,
    factor_hrv INTEGER,
    factor_sleep INTEGER,
    factor_recovery_time INTEGER,
    factor_acwr INTEGER,
    factor_stress_history INTEGER,
    synced_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS training_status (
    date TEXT PRIMARY KEY,
    status_phrase TEXT,
    acwr REAL,
    acute_load REAL,
    chronic_load REAL,
    synced_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS lactate_threshold (
    date TEXT PRIMARY KEY,
    hr_threshold_running INTEGER,
    speed_threshold_sec_per_m REAL,
    synced_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS max_metrics (
    date TEXT PRIMARY KEY,
    vo2max_run REAL,
    vo2max_cycle REAL,
    synced_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS ftp (
    date TEXT PRIMARY KEY,
    garmin_ftp_watts INTEGER,
    synced_at TEXT,
    raw_json TEXT
);

-- activity_id is Garmin's own ID: a real uniqueness constraint, replacing the old
-- InfluxDB-era client-side time+type+name+distance dedup hack (garmin-fetch-data
-- could write the same activity twice on re-sync — an upsert here makes re-syncing
-- inherently idempotent instead).
CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    start_utc TEXT,
    start_local TEXT,
    type TEXT,
    name TEXT,
    duration_s INTEGER,
    distance_m REAL,
    avg_hr INTEGER,
    max_hr INTEGER,
    calories INTEGER,
    training_load REAL,
    te_aerobic REAL,
    te_anaerobic REAL,
    hr_zone1_s INTEGER,
    hr_zone2_s INTEGER,
    hr_zone3_s INTEGER,
    hr_zone4_s INTEGER,
    hr_zone5_s INTEGER,
    avg_power REAL,
    vo2max REAL,
    synced_at TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
CREATE INDEX IF NOT EXISTS idx_activities_type_date ON activities(type, date);

-- get_activity_splits()'s lapDTOs carry no per-lap sport/type field (verified
-- against a real response) — sport is looked up via activity_id -> activities.type
-- when needed (e.g. filtering for cycling laps), not duplicated onto every lap row.
CREATE TABLE IF NOT EXISTS activity_laps (
    activity_id INTEGER NOT NULL,
    lap_idx INTEGER NOT NULL,
    duration_s INTEGER,
    avg_power REAL,
    avg_hr INTEGER,
    PRIMARY KEY (activity_id, lap_idx)
);

CREATE TABLE IF NOT EXISTS bb_intraday (
    ts INTEGER PRIMARY KEY,
    level INTEGER
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS hrv_intraday (
    ts INTEGER PRIMARY KEY,
    hrv_value REAL
) WITHOUT ROWID;

-- garmin_token, backfill checkpoint/progress, last_sync_at, etc. — one place for
-- all sync-process state, avoiding a proliferation of small JSON files.
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    ended_at TEXT,
    kind TEXT,
    days_processed INTEGER,
    errors TEXT
);
"""


def connect() -> sqlite3.Connection:
    """One short-lived connection per call site — coach.py/backfill/sync each get
    their own (separate processes/threads), WAL mode + busy_timeout absorbs the
    resulting concurrency without needing a single shared connection object."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema():
    """Idempotent — CREATE TABLE IF NOT EXISTS throughout, safe to call on every
    process startup. This IS the migration mechanism at this project's scale (single
    user, small schema) — no separate migration framework needed."""
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    """Direct replacement for coach.py's old influx_query() — same list[dict] shape,
    so downstream call sites change minimally when coach.py is rewritten against
    this (Stage 6 of the plan, not yet done)."""
    conn = connect()
    try:
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()):
    """Single-statement write wrapper (INSERT/UPDATE/DELETE), commits immediately."""
    conn = connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def upsert(table: str, key_cols: list[str], row: dict):
    """INSERT ... ON CONFLICT(key_cols) DO UPDATE, built from a plain dict — every
    sync writer uses this so idempotent re-sync is a one-line call, not hand-written
    SQL at every call site. row's keys become the column list; key_cols must be a
    subset of row's keys."""
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    update_cols = [c for c in cols if c not in key_cols]
    conflict_clause = ", ".join(key_cols)
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_clause}) DO UPDATE SET {update_clause}"
    )
    conn = connect()
    try:
        conn.execute(sql, tuple(row[c] for c in cols))
        conn.commit()
    finally:
        conn.close()


def get_sync_state(key: str, default=None):
    rows = query("SELECT value FROM sync_state WHERE key = ?", (key,))
    if not rows:
        return default
    return json.loads(rows[0]["value"])


def set_sync_state(key: str, value):
    upsert("sync_state", ["key"], {"key": key, "value": json.dumps(value)})


if __name__ == "__main__":
    init_schema()
    print(f"Schema initialized at {DB_FILE}")
