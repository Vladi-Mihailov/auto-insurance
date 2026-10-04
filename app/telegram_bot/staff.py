"""The bot's staff: managers and owners, persisted in telegram_bot_staff.

Two roles only:
- manager: everything a customer can do + the manager functions (orders,
  pending payments, order cards, receipts, payment confirm/reject, policy
  upload);
- owner: everything a manager can do + adding/removing managers.

Identity is ALWAYS the Telegram user id (from the incoming update);
username is display metadata only and may change.

Where staff comes from:
- bootstrap() at bot start: TELEGRAM_BOT_MANAGER_IDS (the original,
  env-only configuration) become managers, and the initial owner is
  TELEGRAM_BOT_OWNER_ID -- or, when that is unset and exactly ONE manager
  is configured, that manager. Several configured managers and no explicit
  owner -> no owner is guessed (a warning is logged);
- a one-time invite created by an owner (create_invite / consume_invite):
  the account that actually opens the link becomes a manager -- never an
  owner.

Every check here reads the DB, so a removed manager loses access with the
very next update, and roles/invites survive restarts.
"""

import hashlib
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ROLE_OWNER = "owner"
ROLE_MANAGER = "manager"
SOURCE_CONFIG = "config"
SOURCE_INVITE = "invite"

INVITE_TTL = timedelta(hours=24)
# The /start payload of an invite link: "mgr_" + the token. Telegram allows
# up to 64 chars of [A-Za-z0-9_-] in a start payload.
INVITE_PREFIX = "mgr_"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,60}$")


@dataclass(frozen=True)
class StaffMember:
    telegram_user_id: int
    username: str | None
    role: str
    active: bool
    source: str

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.username else f"ID {self.telegram_user_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(row) -> StaffMember:
    return StaffMember(
        telegram_user_id=row["telegram_user_id"], username=row["username"], role=row["role"],
        active=bool(row["active"]), source=row["source"],
    )


def get_member(conn: sqlite3.Connection, bot_key: str, telegram_user_id: int) -> StaffMember | None:
    row = conn.execute(
        "SELECT * FROM telegram_bot_staff WHERE bot_key = ? AND telegram_user_id = ?", (bot_key, telegram_user_id)
    ).fetchone()
    return _row(row) if row else None


def role_of(conn: sqlite3.Connection, bot_key: str, telegram_user_id: int | None) -> str | None:
    """'owner' / 'manager' for ACTIVE staff, None for everyone else."""
    if telegram_user_id is None:
        return None
    member = get_member(conn, bot_key, telegram_user_id)
    return member.role if member and member.active else None


def is_staff(conn: sqlite3.Connection, bot_key: str, telegram_user_id: int | None) -> bool:
    return role_of(conn, bot_key, telegram_user_id) is not None


def is_owner(conn: sqlite3.Connection, bot_key: str, telegram_user_id: int | None) -> bool:
    return role_of(conn, bot_key, telegram_user_id) == ROLE_OWNER


def active_staff(conn: sqlite3.Connection, bot_key: str) -> list[StaffMember]:
    """Owners first, then managers, each in the order they joined."""
    rows = conn.execute(
        """SELECT * FROM telegram_bot_staff WHERE bot_key = ? AND active = 1
           ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, id""",
        (bot_key,),
    ).fetchall()
    return [_row(r) for r in rows]


def active_ids(conn: sqlite3.Connection, bot_key: str) -> frozenset[int]:
    """Everyone who verifies payments (owners included) -- each id once."""
    return frozenset(m.telegram_user_id for m in active_staff(conn, bot_key))


def _upsert(conn, bot_key, telegram_user_id, *, role, source, created_by=None, username=None) -> None:
    now = _now().isoformat()
    conn.execute(
        """INSERT INTO telegram_bot_staff (bot_key, telegram_user_id, username, role, active, source, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
           ON CONFLICT (bot_key, telegram_user_id) DO UPDATE SET
               role = excluded.role, active = 1, source = excluded.source,
               created_by = excluded.created_by, username = COALESCE(excluded.username, username),
               updated_at = excluded.updated_at""",
        (bot_key, telegram_user_id, username, role, source, created_by, now, now),
    )


def bootstrap(conn: sqlite3.Connection, bot_key: str, manager_ids, owner_id: int | None = None) -> int | None:
    """Sync the env configuration into the staff table (idempotent; run at
    every bot start). Returns the owner chosen NOW, if one was made.

    - a configured manager id without a row -> active manager;
    - a config-sourced MANAGER no longer configured -> deactivated (removing
      an id from TELEGRAM_BOT_MANAGER_IDS still revokes it, as before);
    - a row the owner deactivated stays deactivated (the owner's decision
      wins over the env list);
    - no active owner yet -> owner_id, else the ONLY configured manager."""
    manager_ids = frozenset(manager_ids)
    now = _now().isoformat()
    existing = {
        row["telegram_user_id"]: row
        for row in conn.execute("SELECT * FROM telegram_bot_staff WHERE bot_key = ?", (bot_key,)).fetchall()
    }
    for user_id in sorted(manager_ids - existing.keys()):
        _upsert(conn, bot_key, user_id, role=ROLE_MANAGER, source=SOURCE_CONFIG)
    for user_id, row in existing.items():
        if row["source"] == SOURCE_CONFIG and row["role"] == ROLE_MANAGER and row["active"] and user_id not in manager_ids:
            conn.execute(
                "UPDATE telegram_bot_staff SET active = 0, updated_at = ? WHERE bot_key = ? AND telegram_user_id = ?",
                (now, bot_key, user_id),
            )
    chosen = None
    if owner_id is not None and not is_owner(conn, bot_key, owner_id):
        chosen = owner_id  # explicitly configured: always made an (active) owner
    elif owner_id is None and not _active_owner_count(conn, bot_key):
        if len(manager_ids) == 1:
            chosen = next(iter(manager_ids))
        else:
            logger.warning(
                "No bot owner for bot_key=%s: set TELEGRAM_BOT_OWNER_ID to choose one among the %d configured managers",
                bot_key, len(manager_ids),
            )
    if chosen is not None:
        _upsert(conn, bot_key, chosen, role=ROLE_OWNER, source=SOURCE_CONFIG)
        logger.info("Bot owner bootstrapped for bot_key=%s", bot_key)
    conn.commit()
    return chosen


def _active_owner_count(conn, bot_key) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM telegram_bot_staff WHERE bot_key = ? AND role = 'owner' AND active = 1", (bot_key,)
    ).fetchone()[0]


def touch_username(conn: sqlite3.Connection, bot_key: str, telegram_user_id: int, username: str | None) -> None:
    """Refresh the display username of a staff member (it may change)."""
    cursor = conn.execute(
        """UPDATE telegram_bot_staff SET username = ?, updated_at = ?
           WHERE bot_key = ? AND telegram_user_id = ? AND username IS NOT ?""",
        (username, _now().isoformat(), bot_key, telegram_user_id, username),
    )
    if cursor.rowcount:
        conn.commit()


# ---------------------------------------------------------------- invites


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_invite(conn: sqlite3.Connection, bot_key: str, *, created_by: int) -> tuple[str, datetime]:
    """(token, expires_at). Only the token's hash is stored -- the token
    itself exists only in the link handed to the owner."""
    token = secrets.token_urlsafe(24)  # 192 bits, 32 url-safe chars
    now = _now()
    expires = now + INVITE_TTL
    conn.execute(
        "INSERT INTO telegram_bot_staff_invites (bot_key, token_hash, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (bot_key, _hash(token), created_by, now.isoformat(), expires.isoformat()),
    )
    conn.commit()
    return token, expires


def invite_token(payload: str | None) -> str | None:
    """The token of an invite /start payload, None for anything else."""
    if not payload or not payload.startswith(INVITE_PREFIX):
        return None
    return payload[len(INVITE_PREFIX):]


@dataclass(frozen=True)
class InviteResult:
    status: str  # "added" | "already_staff" | "invalid"
    created_by: int | None = None


def consume_invite(
    conn: sqlite3.Connection, bot_key: str, token: str, *, telegram_user_id: int, username: str | None
) -> InviteResult:
    """Bind a valid, unexpired, unused invite to THIS Telegram user as a
    manager. The token is consumed by a single conditional UPDATE, so two
    accounts racing for one link can't both win. An account that already is
    active staff doesn't use up the link (it stays valid for its invitee)."""
    if not _TOKEN_RE.fullmatch(token or ""):
        return InviteResult("invalid")
    if is_staff(conn, bot_key, telegram_user_id):
        return InviteResult("already_staff")
    now = _now().isoformat()
    token_hash = _hash(token)
    cursor = conn.execute(
        """UPDATE telegram_bot_staff_invites SET consumed_at = ?, consumed_by = ?
           WHERE bot_key = ? AND token_hash = ? AND consumed_at IS NULL AND expires_at > ?""",
        (now, telegram_user_id, bot_key, token_hash, now),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        return InviteResult("invalid")
    row = conn.execute(
        "SELECT id, created_by FROM telegram_bot_staff_invites WHERE bot_key = ? AND token_hash = ?", (bot_key, token_hash)
    ).fetchone()
    # Always 'manager' -- an invite never grants (or keeps) owner rights.
    _upsert(conn, bot_key, telegram_user_id, role=ROLE_MANAGER, source=SOURCE_INVITE, created_by=row["created_by"], username=username)
    conn.commit()
    logger.info("Manager invite %s consumed (bot_key=%s)", row["id"], bot_key)
    return InviteResult("added", created_by=row["created_by"])


# ---------------------------------------------------------------- removal


def deactivate_manager(conn: sqlite3.Connection, bot_key: str, *, actor_id: int, target_id: int) -> str:
    """Owner-only soft removal of a MANAGER.
    "removed" | "not_owner" | "self" | "is_owner" | "not_found".
    Owners are never removed here -- so the last owner can't be lost."""
    if not is_owner(conn, bot_key, actor_id):
        return "not_owner"
    if target_id == actor_id:
        return "self"
    member = get_member(conn, bot_key, target_id)
    if member is None or not member.active:
        return "not_found"
    if member.role == ROLE_OWNER:
        return "is_owner"
    cursor = conn.execute(
        """UPDATE telegram_bot_staff SET active = 0, updated_at = ?
           WHERE bot_key = ? AND telegram_user_id = ? AND role = 'manager' AND active = 1""",
        (_now().isoformat(), bot_key, target_id),
    )
    conn.commit()
    return "removed" if cursor.rowcount == 1 else "not_found"
