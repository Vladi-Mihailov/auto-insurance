"""aiogram FSM storage backed by insurance_sessions.conversation_state.

Holds ONLY conversation navigation -- which question the bot is waiting for
an answer to, plus a few navigation hints (NAVIGATION_KEYS: where an edit
should return to, which page of a list is showing, the text being searched
for). It lives on the same insurance_sessions row as the customer's draft,
so it survives a bot restart and there is one row per customer, not two
stores. Anything else -- in particular any business data (VIN, names,
prices...) -- is rejected by set_data: that belongs in the draft
(app.sessions) and nowhere else.

Column format: JSON {"state": ..., "nav": {...}}. A plain string (the
phase-3 format, state only) is still read correctly.
"""

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from app.db import get_connection
from app.sessions.repository import ensure_session
from app.telegram_bot.sessions import session_id_for

NAVIGATION_KEYS = frozenset({"return_to", "page", "query"})
_MAX_NAV_TEXT = 64


class ConversationDataNotAllowed(ValueError):
    pass


def _validate_nav(data: Mapping[str, Any]) -> dict[str, Any]:
    nav = {}
    for key, value in data.items():
        if key not in NAVIGATION_KEYS:
            raise ConversationDataNotAllowed(f"{key!r} is not navigation state -- business data belongs in the draft")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ConversationDataNotAllowed(f"navigation value for {key!r} must be a short string or an int")
        if isinstance(value, str):
            value = value[:_MAX_NAV_TEXT]
        nav[key] = value
    return nav


class SessionConversationStorage(BaseStorage):
    def __init__(self, db_file: Path, bot_key: str):
        self._db_file = db_file
        self._bot_key = bot_key

    def _session_id(self, key: StorageKey) -> str:
        # Customer flows only run in private chats (see app.telegram_bot.
        # handlers), where chat id == user id; the user id is the identity.
        return session_id_for(self._bot_key, key.user_id)

    def _read(self, key: StorageKey) -> dict:
        conn = get_connection(self._db_file)
        try:
            row = conn.execute(
                "SELECT conversation_state FROM insurance_sessions WHERE session_id = ?", (self._session_id(key),)
            ).fetchone()
        finally:
            conn.close()
        raw = row["conversation_state"] if row else None
        if not raw:
            return {"state": None, "nav": {}}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                return {"state": parsed.get("state"), "nav": dict(parsed.get("nav") or {})}
            except (ValueError, AttributeError):
                return {"state": None, "nav": {}}
        return {"state": raw, "nav": {}}  # phase-3 plain-string format

    def _write(self, key: StorageKey, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False) if (value["state"] or value["nav"]) else None
        conn = get_connection(self._db_file)
        try:
            session_id = self._session_id(key)
            ensure_session(conn, session_id)
            conn.execute(
                "UPDATE insurance_sessions SET conversation_state = ?, last_seen_at = ? WHERE session_id = ?",
                (payload, datetime.now(timezone.utc).isoformat(), session_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        current = self._read(key)
        current["state"] = state.state if isinstance(state, State) else state
        self._write(key, current)

    async def get_state(self, key: StorageKey) -> str | None:
        return self._read(key)["state"]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        current = self._read(key)
        current["nav"] = _validate_nav(data)
        self._write(key, current)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return self._read(key)["nav"]

    async def close(self) -> None:
        return None
