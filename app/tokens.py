"""Non-guessable identifiers for anonymous sessions and order resume links."""

import secrets


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def new_resume_token() -> str:
    return secrets.token_urlsafe(32)
