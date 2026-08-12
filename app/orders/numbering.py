"""Human-readable order numbers.

Derived from the DB row id — guaranteed unique for free, no separate
counter table needed. This is NOT a secret (see resume_token for that).
"""


def public_number(order_id: int) -> str:
    return f"ORDER-{1000 + order_id}"
