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
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    resting_hr INTEGER,
    steps INTEGER,
    stress_avg REAL,
    bb_high INTEGER,
    bb_low INTEGER,
    synced_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS sleep (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    sleep_seconds INTEGER,
    sleep_score INTEGER,
    deep_s INTEGER,
    light_s INTEGER,
    rem_s INTEGER,
    awake_s INTEGER,
    synced_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (user_id, date)
);

-- Column names follow get_hrv_data()'s real hrvSummary.baseline shape (verified
-- against a live response): balancedLow/balancedUpper is the BALANCED band Garmin
-- itself uses for the "BALANCED" status; lowUpper is a separate, lower cutoff.
CREATE TABLE IF NOT EXISTS hrv (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    last_night_avg INTEGER,
    weekly_avg INTEGER,
    status TEXT,
    baseline_low_upper INTEGER,
    baseline_balanced_low INTEGER,
    baseline_balanced_upper INTEGER,
    synced_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS training_readiness (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
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
    raw_json TEXT,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS training_status (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    status_phrase TEXT,
    acwr REAL,
    acute_load REAL,
    chronic_load REAL,
    synced_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS lactate_threshold (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    hr_threshold_running INTEGER,
    speed_threshold_sec_per_m REAL,
    synced_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (user_id, date)
);

-- vo2max_run/vo2max_cycle are Garmin's rounded whole-number vo2MaxValue; the
-- _precise columns are vo2MaxPreciseValue (e.g. 51.5 vs. the rounded 51.0) —
-- Garmin's API genuinely returns both, not a derived/computed distinction.
CREATE TABLE IF NOT EXISTS max_metrics (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    vo2max_run REAL,
    vo2max_run_precise REAL,
    vo2max_cycle REAL,
    vo2max_cycle_precise REAL,
    synced_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS ftp (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    garmin_ftp_watts INTEGER,
    synced_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (user_id, date)
);

-- activity_id is Garmin's own ID, unique per Garmin account — combined with user_id
-- it stays a real uniqueness constraint (replacing the old InfluxDB-era client-side
-- time+type+name+distance dedup hack) even though two different users' Garmin
-- accounts could in principle both have an activity_id 1 (unlikely, Garmin's ids
-- are large/effectively global, but user_id in the key costs nothing and removes
-- the assumption entirely).
CREATE TABLE IF NOT EXISTS activities (
    user_id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
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
    raw_json TEXT,
    PRIMARY KEY (user_id, activity_id)
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(user_id, date);
CREATE INDEX IF NOT EXISTS idx_activities_type_date ON activities(user_id, type, date);

-- get_activity_splits()'s lapDTOs carry no per-lap sport/type field (verified
-- against a real response) — sport is looked up via activity_id -> activities.type
-- when needed (e.g. filtering for cycling laps), not duplicated onto every lap row.
CREATE TABLE IF NOT EXISTS activity_laps (
    user_id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    lap_idx INTEGER NOT NULL,
    duration_s INTEGER,
    avg_power REAL,
    avg_hr INTEGER,
    PRIMARY KEY (user_id, activity_id, lap_idx)
);

CREATE TABLE IF NOT EXISTS bb_intraday (
    user_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    level INTEGER,
    PRIMARY KEY (user_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS hrv_intraday (
    user_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    hrv_value REAL,
    PRIMARY KEY (user_id, ts)
) WITHOUT ROWID;

-- garmin_token, backfill checkpoint/progress, last_sync_at, etc. — one place for
-- all sync-process state, avoiding a proliferation of small JSON files. user_id=0
-- is reserved for state that predates/isn't scoped to any user (none currently).
CREATE TABLE IF NOT EXISTS sync_state (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    kind TEXT,
    days_processed INTEGER,
    errors TEXT
);

-- Per-user dashboard/coach settings (language, timezone, watch device, Discord
-- webhook, daily advice time). Replaces the old single flat coach_settings.json —
-- reuses upsert()/query() exactly like sync_state already does, one row per
-- (user_id, key) so new setting keys need no schema migration to add.
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (user_id, key)
);

-- Multi-user support (Stage 1 of the multi-user plan). session_owner_id defaults
-- to the user's own id at registration (own AI session); it's repointed at
-- another user's id when a share code is redeemed, so "whose tmux pane/lock/
-- output-file does this user's prompt use" is a lookup, not the user's own id.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    session_owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Admin-generated, single-use, one per invitee — redeeming creates the account.
CREATE TABLE IF NOT EXISTS invites (
    token TEXT PRIMARY KEY,
    created_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_by_user_id INTEGER,
    used_at TEXT
);

-- Reusable until revoked (owner can share the same code with several people) —
-- redeeming sets the borrower's users.session_owner_id to owner_user_id.
CREATE TABLE IF NOT EXISTS share_codes (
    code TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

-- Who is currently borrowing a share code, for attribution/per-borrower revoke
-- without needing a separate code per person.
CREATE TABLE IF NOT EXISTS session_shares (
    code TEXT NOT NULL,
    borrower_user_id INTEGER NOT NULL,
    redeemed_at TEXT NOT NULL,
    PRIMARY KEY (code, borrower_user_id)
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


def get_sync_state(user_id: int, key: str, default=None):
    rows = query("SELECT value FROM sync_state WHERE user_id = ? AND key = ?", (user_id, key))
    if not rows:
        return default
    return json.loads(rows[0]["value"])


def set_sync_state(user_id: int, key: str, value):
    upsert("sync_state", ["user_id", "key"], {"user_id": user_id, "key": key, "value": json.dumps(value)})


def get_setting(user_id: int, key: str, default=None):
    rows = query("SELECT value FROM settings WHERE user_id = ? AND key = ?", (user_id, key))
    if not rows:
        return default
    return json.loads(rows[0]["value"])


def set_setting(user_id: int, key: str, value):
    upsert("settings", ["user_id", "key"], {"user_id": user_id, "key": key, "value": json.dumps(value)})


if __name__ == "__main__":
    init_schema()
    print(f"Schema initialized at {DB_FILE}")
