"""Shared FastAPI dependencies: settings, DB connection, anonymous session, order lookup."""

import sqlite3
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from app import tokens
from app.db import get_connection
from app.orders.models import Order
from app.orders.repository import get_order_by_token
from app.sessions.repository import ensure_session
from app.settings import Settings, load_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_COOKIE_NAME = "ai_session"
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 180  # 180 days


@lru_cache
def get_settings() -> Settings:
    return load_settings(PROJECT_ROOT)


def get_db():
    settings = get_settings()
    conn = get_connection(settings.app.db_file)
    try:
        yield conn
    finally:
        conn.close()


def get_session_id(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> str:
    """Reads/creates the anonymous session id.

    Route handlers here always return their own Response object (a
    TemplateResponse or a RedirectResponse) rather than plain data, so
    FastAPI's usual "set cookies on the injected Response param" trick does
    not apply — it only merges cookies when the endpoint returns plain data
    that FastAPI wraps itself. Instead, a newly generated session_id is
    stashed on request.state, and SessionCookieMiddleware (see main.py)
    applies it to whatever response actually comes back.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        session_id = tokens.new_session_id()
        request.state.new_session_id = session_id
    ensure_session(conn, session_id)
    return session_id


def get_order_or_404(resume_token: str, conn: sqlite3.Connection = Depends(get_db)) -> Order:
    order = get_order_by_token(conn, resume_token)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
