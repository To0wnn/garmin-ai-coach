#!/usr/bin/env python3
"""Password auth + server-side session cookies for the multi-user dashboard.
Stdlib only (hashlib/secrets) — matches the project's existing minimal-deps
convention (garminconnect is the only pip dependency). No OAuth/JWT: this is a
homelab-scale (a handful of users) local-network-only deployment, so a plain
opaque session token stored server-side is simpler to reason about than a
signed/stateless token would be, and revocation is just a DELETE.

Used by dashboard.py (session-cookie auth on every handler), cron_dispatch.py
(iterates all users), migrate_to_multiuser.py (creates the initial admin), and
entrypoint.sh (via list_distinct_session_owners(), below, for the boot-time
per-owner tmux session loop)."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import db

SESSION_TTL = timedelta(days=30)
PBKDF2_ITERATIONS = 200_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Returns (hash_hex, salt_hex). Pass salt=None to generate a fresh one
    (registration); pass the stored salt back in to verify a login attempt."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, password_salt: str) -> bool:
    candidate, _ = hash_password(password, password_salt)
    return secrets.compare_digest(candidate, password_hash)


def create_user(username: str, password: str, is_admin: bool = False) -> dict:
    """Raises sqlite3.IntegrityError if username is taken (UNIQUE constraint —
    let the caller catch it rather than pre-checking, avoids a TOCTOU gap)."""
    password_hash, password_salt = hash_password(password)
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, password_salt, is_admin, session_owner_id, created_at) "
            "VALUES (?, ?, ?, ?, -1, ?)",
            (username, password_hash, password_salt, int(is_admin), _now_iso()),
        )
        user_id = cur.lastrowid
        # session_owner_id defaults to the user's own id — can't know it until
        # the row exists (autoincrement), so insert with a placeholder then fix up.
        conn.execute("UPDATE users SET session_owner_id = ? WHERE id = ?", (user_id, user_id))
        conn.commit()
    finally:
        conn.close()
    return get_user_by_id(user_id)


def get_user_by_id(user_id: int) -> dict | None:
    rows = db.query("SELECT * FROM users WHERE id = ?", (user_id,))
    return rows[0] if rows else None


def get_user_by_username(username: str) -> dict | None:
    rows = db.query("SELECT * FROM users WHERE username = ?", (username,))
    return rows[0] if rows else None


def list_distinct_session_owners() -> list[int]:
    """Every distinct AI-session owner_id currently in use — i.e. every
    session_owner_id value that at least one user points at. Used at
    container boot to start exactly one tmux session per owner (not one per
    user — a user borrowing another's session via a share code shouldn't get
    their own idle session started too)."""
    rows = db.query("SELECT DISTINCT session_owner_id FROM users ORDER BY session_owner_id")
    return [r["session_owner_id"] for r in rows]


def authenticate(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"], user["password_salt"]):
        return None
    return user


def change_password(user_id: int, current_password: str, new_password: str) -> bool:
    """Requires the current password (not just being logged in) so a
    hijacked session can't silently lock the real owner out by changing it
    to something else. Returns False if current_password doesn't match."""
    user = get_user_by_id(user_id)
    if user is None or not verify_password(current_password, user["password_hash"], user["password_salt"]):
        return False
    new_hash, new_salt = hash_password(new_password)
    db.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (new_hash, new_salt, user_id))
    return True


def list_users() -> list[dict]:
    """For the admin user-list page — deliberately excludes password_hash/
    password_salt from what's typically shown, but callers get the full row
    (this is only reachable by an admin already, no need for a second
    field-filtered variant)."""
    return db.query("SELECT * FROM users ORDER BY created_at")


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now.isoformat(), (now + SESSION_TTL).isoformat()),
    )
    return token


def get_user_by_session(token: str) -> dict | None:
    rows = db.query(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
        "WHERE sessions.token = ? AND sessions.expires_at > ?",
        (token, _now_iso()),
    )
    return rows[0] if rows else None


def delete_session(token: str):
    db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def session_owner_id_of(user: dict) -> int:
    """The effective AI-session owner for this user — their own id unless
    they've redeemed a share code, in which case it's the code owner's id."""
    return user["session_owner_id"]


def set_session_owner(user_id: int, owner_user_id: int):
    db.execute("UPDATE users SET session_owner_id = ? WHERE id = ?", (owner_user_id, user_id))


def revert_to_own_session(user_id: int):
    set_session_owner(user_id, user_id)


# ---------- invites (admin-generated, single-use) ----------

INVITE_TTL = timedelta(days=7)


def create_invite(created_by_user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO invites (token, created_by_user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, created_by_user_id, now.isoformat(), (now + INVITE_TTL).isoformat()),
    )
    return token


def redeem_invite(token: str, username: str, password: str) -> dict | None:
    """Returns the new user dict, or None if the invite is missing/expired/used."""
    rows = db.query("SELECT * FROM invites WHERE token = ?", (token,))
    if not rows:
        return None
    invite = rows[0]
    if invite["used_by_user_id"] is not None or invite["expires_at"] <= _now_iso():
        return None
    user = create_user(username, password, is_admin=False)
    db.execute(
        "UPDATE invites SET used_by_user_id = ?, used_at = ? WHERE token = ?",
        (user["id"], _now_iso(), token),
    )
    return user


# ---------- share codes (reusable until revoked) ----------


def create_share_code(owner_user_id: int, label: str = "") -> str:
    code = secrets.token_urlsafe(9)
    db.execute(
        "INSERT INTO share_codes (code, owner_user_id, label, created_at) VALUES (?, ?, ?, ?)",
        (code, owner_user_id, label, _now_iso()),
    )
    return code


def redeem_share_code(borrower_user_id: int, code: str) -> dict | None:
    """Repoints the borrower's session_owner_id at the code's owner. Returns
    the share_codes row, or None if the code is unknown/revoked."""
    rows = db.query("SELECT * FROM share_codes WHERE code = ? AND revoked_at IS NULL", (code,))
    if not rows:
        return None
    share = rows[0]
    set_session_owner(borrower_user_id, share["owner_user_id"])
    db.upsert(
        "session_shares",
        ["code", "borrower_user_id"],
        {"code": code, "borrower_user_id": borrower_user_id, "redeemed_at": _now_iso()},
    )
    return share


def revoke_share_code(owner_user_id: int, code: str) -> bool:
    """Revokes the code and reverts every current borrower back to their own
    session, so nobody is left silently pointing at a dead share. Returns
    False if the code doesn't belong to owner_user_id."""
    rows = db.query("SELECT * FROM share_codes WHERE code = ? AND owner_user_id = ?", (code, owner_user_id))
    if not rows:
        return False
    db.execute("UPDATE share_codes SET revoked_at = ? WHERE code = ?", (_now_iso(), code))
    borrowers = db.query("SELECT borrower_user_id FROM session_shares WHERE code = ?", (code,))
    for row in borrowers:
        revert_to_own_session(row["borrower_user_id"])
    return True


def list_share_codes_for_owner(owner_user_id: int) -> list[dict]:
    codes = db.query(
        "SELECT * FROM share_codes WHERE owner_user_id = ? AND revoked_at IS NULL ORDER BY created_at",
        (owner_user_id,),
    )
    for c in codes:
        c["borrowers"] = db.query(
            "SELECT session_shares.borrower_user_id, session_shares.redeemed_at, users.username "
            "FROM session_shares JOIN users ON users.id = session_shares.borrower_user_id "
            "WHERE session_shares.code = ?",
            (c["code"],),
        )
    return codes


if __name__ == "__main__":
    # ponytail: smallest thing that fails if the logic breaks, not a full test suite.
    db.init_schema()
    import uuid

    uname = f"selftest-{uuid.uuid4().hex[:8]}"
    u = create_user(uname, "correct horse battery staple")
    assert u["session_owner_id"] == u["id"], "new user should default to owning their own session"
    assert authenticate(uname, "correct horse battery staple") is not None
    assert authenticate(uname, "wrong password") is None

    token = create_session(u["id"])
    fetched = get_user_by_session(token)
    assert fetched is not None and fetched["id"] == u["id"]
    delete_session(token)
    assert get_user_by_session(token) is None

    admin = create_user(f"admin-{uuid.uuid4().hex[:8]}", "adminpass", is_admin=True)
    inv_token = create_invite(admin["id"])
    invitee = redeem_invite(inv_token, f"invitee-{uuid.uuid4().hex[:8]}", "invitepass")
    assert invitee is not None
    assert redeem_invite(inv_token, "shouldfail", "x") is None, "invite must be single-use"

    code = create_share_code(admin["id"], label="test share")
    share = redeem_share_code(u["id"], code)
    assert share is not None
    assert get_user_by_id(u["id"])["session_owner_id"] == admin["id"]
    assert revoke_share_code(admin["id"], code) is True
    assert get_user_by_id(u["id"])["session_owner_id"] == u["id"], "revoke should revert borrower to own session"

    print("auth.py self-check passed")
