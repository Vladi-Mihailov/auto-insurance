"""Minimal funnel analytics — one SQLite table, no external analytics stack."""

import json
import sqlite3
from datetime import datetime, timezone


def log_event(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    order_id: int | None,
    event_name: str,
    properties: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO insurance_analytics_events (session_id, order_id, event_name, properties, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            session_id,
            order_id,
            event_name,
            json.dumps(properties) if properties else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
