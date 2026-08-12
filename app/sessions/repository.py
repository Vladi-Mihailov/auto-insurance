"""Anonymous browser session + pre-order draft storage.

A session exists before any order does — it's how we correlate analytics
events (and, later, chat messages) to a browser without any login. The
vehicle form is filled in before the contact step creates the order, so its
values are held here as a JSON draft keyed by session_id and consumed once
the order is created.
"""

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_session(conn: sqlite3.Connection, session_id: str) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO insurance_sessions (session_id, created_at, last_seen_at)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
        """,
        (session_id, now, now),
    )
    conn.commit()


def save_draft(conn: sqlite3.Connection, session_id: str, data: dict) -> None:
    conn.execute(
        "UPDATE insurance_sessions SET draft_data = ?, last_seen_at = ? WHERE session_id = ?",
        (json.dumps(data), _now(), session_id),
    )
    conn.commit()


def get_draft(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute(
        "SELECT draft_data FROM insurance_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None or row["draft_data"] is None:
        return None
    return json.loads(row["draft_data"])


def merge_draft(conn: sqlite3.Connection, session_id: str, updates: dict) -> dict:
    """Reads the current draft (if any), applies updates on top, saves and
    returns the result. The pre-order checkout has several steps (category
    +period, dates, method, vehicle data) that each contribute a few keys —
    this lets every step just add its own fields without clobbering what
    earlier steps already stored."""
    draft = get_draft(conn, session_id) or {}
    draft.update(updates)
    save_draft(conn, session_id, draft)
    return draft


def clear_draft(conn: sqlite3.Connection, session_id: str) -> None:
    """Called once the draft has been consumed into a real order (see
    app.web.checkout_routes.post_policyholder). Without this, a resubmitted
    /policyholder POST (browser back + resubmit, double-click, etc.) would
    still find a complete draft and create a second, duplicate order —
    clearing it makes a resubmit fail the pre-order guard instead."""
    save_draft(conn, session_id, {})
