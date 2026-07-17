#!/usr/bin/env python3
"""One-time migration: rewrites every pre-multi-user data table to add a
user_id column and promote the primary key to (user_id, ...), tagging all
existing rows user_id=1 (the pre-existing single user becomes user #1).

SQLite can't ALTER TABLE to change a primary key in place, so each table is
rebuilt: create <table>_new with the new schema, copy rows across with
user_id=1 injected, drop the old table, rename _new back to the original
name. db.py's _SCHEMA already defines the target (multi-user) shape via
CREATE TABLE IF NOT EXISTS, so this script's job is just getting an
old-shape table into that shape before init_schema() runs — once migrated,
init_schema()'s IF NOT EXISTS is a no-op here forever after.

Idempotent-guarded via sync_state (user_id=1, key=schema_v2_migrated) so a
container restart doesn't try to re-run this. Safe to run against a fresh
install too (every table it looks for either won't exist yet, in which case
it's skipped, or will already be in the new shape, in which case the
"does old-shape table exist" check below is false)."""

import os
import sqlite3
import sys

import db

# (table, old_pk_cols, all_data_cols_excluding_user_id_in_final_order)
# all_data_cols must match db.py's _SCHEMA column order for that table minus
# user_id, since the copy is a straight positional SELECT.
_TABLES = [
    ("daily_summary", ["date"], ["date", "resting_hr", "steps", "stress_avg", "bb_high", "bb_low", "synced_at", "raw_json"]),
    ("sleep", ["date"], ["date", "sleep_seconds", "sleep_score", "deep_s", "light_s", "rem_s", "awake_s", "synced_at", "raw_json"]),
    ("hrv", ["date"], ["date", "last_night_avg", "weekly_avg", "status", "baseline_low_upper", "baseline_balanced_low", "baseline_balanced_upper", "synced_at", "raw_json"]),
    ("training_readiness", ["date"], ["date", "score", "level", "acute_load", "recovery_time_min", "sleep_score", "factor_hrv", "factor_sleep", "factor_recovery_time", "factor_acwr", "factor_stress_history", "synced_at", "raw_json"]),
    ("training_status", ["date"], ["date", "status_phrase", "acwr", "acute_load", "chronic_load", "synced_at", "raw_json"]),
    ("lactate_threshold", ["date"], ["date", "hr_threshold_running", "speed_threshold_sec_per_m", "synced_at", "raw_json"]),
    ("max_metrics", ["date"], ["date", "vo2max_run", "vo2max_cycle", "synced_at", "raw_json"]),
    ("ftp", ["date"], ["date", "garmin_ftp_watts", "synced_at", "raw_json"]),
    ("activities", ["activity_id"], ["activity_id", "date", "start_utc", "start_local", "type", "name", "duration_s", "distance_m", "avg_hr", "max_hr", "calories", "training_load", "te_aerobic", "te_anaerobic", "hr_zone1_s", "hr_zone2_s", "hr_zone3_s", "hr_zone4_s", "hr_zone5_s", "avg_power", "vo2max", "synced_at", "raw_json"]),
    ("activity_laps", ["activity_id", "lap_idx"], ["activity_id", "lap_idx", "duration_s", "avg_power", "avg_hr"]),
    ("bb_intraday", ["ts"], ["ts", "level"]),
    ("hrv_intraday", ["ts"], ["ts", "hrv_value"]),
    ("sync_state", ["key"], ["key", "value"]),
    ("sync_log", None, None),  # has its own INTEGER PK id already, just needs user_id added
]

EXISTING_USER_ID = 1
MIGRATED_FLAG_KEY = "schema_v2_migrated"


def _table_needs_migration(conn: sqlite3.Connection, table: str) -> bool:
    """True if the table exists and its PK does NOT already include user_id
    (i.e. it's still in the old single-user shape)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        return False  # table doesn't exist yet — init_schema() will create it fresh, nothing to migrate
    cols = {row[1] for row in rows}
    return "user_id" not in cols


def _migrate_keyed_table(conn: sqlite3.Connection, table: str, old_pk: list[str], data_cols: list[str]):
    if not _table_needs_migration(conn, table):
        print(f"  {table}: already migrated or fresh, skipping")
        return
    print(f"  {table}: migrating...")
    # Read the CREATE TABLE for the _new shape out of db.py's own _SCHEMA so this
    # script can't drift from the real target schema — extract just this table's
    # statement rather than re-declaring columns a second time here.
    stmt = _extract_create_statement(db._SCHEMA, table)
    conn.execute(stmt.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {table}_new", 1))

    col_list = ", ".join(data_cols)
    conn.execute(
        f"INSERT INTO {table}_new (user_id, {col_list}) "
        f"SELECT ?, {col_list} FROM {table}",
        (EXISTING_USER_ID,),
    )
    old_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    new_count = conn.execute(f"SELECT COUNT(*) FROM {table}_new").fetchone()[0]
    assert old_count == new_count, f"{table}: row count mismatch after copy ({old_count} -> {new_count})"

    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    print(f"  {table}: done ({new_count} rows tagged user_id={EXISTING_USER_ID})")


def _migrate_sync_log(conn: sqlite3.Connection):
    table = "sync_log"
    if not _table_needs_migration(conn, table):
        print(f"  {table}: already migrated or fresh, skipping")
        return
    print(f"  {table}: migrating...")
    stmt = _extract_create_statement(db._SCHEMA, table)
    conn.execute(stmt.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {table}_new", 1))
    conn.execute(
        f"INSERT INTO {table}_new (id, user_id, started_at, ended_at, kind, days_processed, errors) "
        f"SELECT id, ?, started_at, ended_at, kind, days_processed, errors FROM {table}",
        (EXISTING_USER_ID,),
    )
    old_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    new_count = conn.execute(f"SELECT COUNT(*) FROM {table}_new").fetchone()[0]
    assert old_count == new_count, f"{table}: row count mismatch after copy ({old_count} -> {new_count})"
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    print(f"  {table}: done ({new_count} rows tagged user_id={EXISTING_USER_ID})")


def _extract_create_statement(schema_sql: str, table: str) -> str:
    """Extracts one full CREATE TABLE statement (up to and including its
    terminating ';') out of db.py's _SCHEMA, so this migration's target shape
    can never drift from the real schema. Not just the first ');' after the
    marker — bb_intraday/hrv_intraday have a WITHOUT ROWID suffix after their
    column-list close-paren, so the real terminator is the next ';'."""
    marker = f"CREATE TABLE IF NOT EXISTS {table} "
    start = schema_sql.index(marker)
    end = schema_sql.index(";", start) + 1
    return schema_sql[start:end]


def run():
    conn = db.connect()
    try:
        # sync_state itself is one of the tables being migrated, so the "already
        # done" check can only trust this flag once sync_state is in the new
        # (user_id, key) shape — if it isn't yet, migration clearly hasn't run.
        already_done = False
        if _table_has_user_id(conn, "sync_state"):
            row = conn.execute(
                "SELECT 1 FROM sync_state WHERE user_id = ? AND key = ?",
                (EXISTING_USER_ID, MIGRATED_FLAG_KEY),
            ).fetchone()
            already_done = row is not None

        if already_done:
            print("Multi-user schema migration already completed, skipping schema step.")
        else:
            print("Migrating coach.db to multi-user schema...")
            for table, old_pk, data_cols in _TABLES:
                if table == "sync_log":
                    _migrate_sync_log(conn)
                else:
                    _migrate_keyed_table(conn, table, old_pk, data_cols)
            conn.commit()

            # Run the rest of _SCHEMA (users/sessions/invites/share_codes/session_shares/
            # settings — all brand new, IF NOT EXISTS is correct for them) now that every
            # pre-existing table is in the new shape.
            conn.executescript(db._SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (user_id, key, value) VALUES (?, ?, ?)",
                (EXISTING_USER_ID, MIGRATED_FLAG_KEY, "true"),
            )
            conn.commit()
            print("Migration complete.")
    finally:
        conn.close()

    # Deliberately OUTSIDE the already_done branch above: admin-account
    # creation and legacy-file migration have their own independent
    # idempotency checks (existing user id=1 / file-already-copied), so an
    # operator who re-runs this script after belatedly setting
    # INITIAL_ADMIN_USERNAME/PASSWORD still gets the admin account created,
    # rather than the whole script short-circuiting on the schema flag alone.
    _create_admin_account()
    _migrate_legacy_files()


def _create_admin_account():
    """Creates the id=1 admin account for the pre-existing (now-migrated) user.
    Idempotent — skips if a row with id=1 already exists (either from a
    previous run of this script, or because init_schema()/auth.create_user
    already created one some other way)."""
    import auth

    existing = auth.get_user_by_id(EXISTING_USER_ID)
    if existing is not None:
        print(f"User id={EXISTING_USER_ID} already exists ({existing['username']}), skipping admin creation.")
        return

    username = os.environ.get("INITIAL_ADMIN_USERNAME")
    password = os.environ.get("INITIAL_ADMIN_PASSWORD")
    if not username or not password:
        print(
            "WARNING: INITIAL_ADMIN_USERNAME/INITIAL_ADMIN_PASSWORD not set — "
            "skipping admin account creation. Set both and re-run this script "
            "to create the admin login for the dashboard.",
            file=sys.stderr,
        )
        return

    user = auth.create_user(username, password, is_admin=True)
    # id should be 1 since this runs on a freshly-migrated DB with no other
    # users yet — assert rather than silently trusting it, since everything
    # downstream (existing data's user_id=1 tagging) depends on this holding.
    assert user["id"] == EXISTING_USER_ID, (
        f"expected the first created user to get id={EXISTING_USER_ID}, got {user['id']} — "
        "the users table already had rows before this ran?"
    )
    print(f"Created admin account '{username}' (id={user['id']}).")


def _migrate_legacy_files():
    """Copies the pre-multi-user ~/coach_settings.json into the new per-user
    settings table, MOVES ~/coach_log.json into coach.owner_log_file(1) (the
    coach's own per-user advice-history path — coach_log.json used to be a
    single shared file, which turned out to leak user 1's real advice history
    to every other user via /api/data until this move + the code-level
    per-user fix were both made; see the multi-user plan's forge-verification
    notes), and MOVES the existing AI-provider credentials (~/.claude.json,
    ~/.claude/, ~/.gemini/) into session_manager.owner_home(1) — session_
    manager.py now runs the tmux session's CLI with HOME=owner_home(owner_id)
    instead of the container-wide /home/coach, so without this move the
    pre-existing login would appear lost (the CLI would look in the new
    per-owner home and find nothing there)."""
    import json
    import shutil

    import coach
    import session_manager
    import settings as settings_module

    legacy_settings_file = os.path.expanduser("~/coach_settings.json")
    if os.path.exists(legacy_settings_file):
        with open(legacy_settings_file) as f:
            legacy = json.load(f)
        settings_module.write_settings(EXISTING_USER_ID, legacy)
        print(f"Migrated {legacy_settings_file} into the settings table for user_id={EXISTING_USER_ID}.")
    else:
        print(f"No legacy {legacy_settings_file} found — nothing to migrate (fresh install, or already migrated).")

    legacy_log_file = os.path.expanduser("~/coach_log.json")
    new_log_file = coach.owner_log_file(EXISTING_USER_ID)
    if os.path.exists(new_log_file):
        print(f"{new_log_file} already exists — skipping (already migrated).")
    elif os.path.exists(legacy_log_file):
        shutil.move(legacy_log_file, new_log_file)
        print(f"Moved {legacy_log_file} -> {new_log_file} (advice history, now per-user).")

    owner_home = session_manager.owner_home(EXISTING_USER_ID)
    os.makedirs(owner_home, exist_ok=True)
    for name in (".claude.json", ".claude", ".gemini"):
        old_path = os.path.expanduser(f"~/{name}")
        new_path = os.path.join(owner_home, name)
        if os.path.exists(new_path):
            print(f"{new_path} already exists — skipping (already migrated).")
            continue
        if not os.path.exists(old_path):
            continue
        shutil.move(old_path, new_path)
        print(f"Moved {old_path} -> {new_path} (AI-provider credentials, now owner-scoped).")


def _table_has_user_id(conn: sqlite3.Connection, table: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == "user_id" for row in rows)


if __name__ == "__main__":
    run()
