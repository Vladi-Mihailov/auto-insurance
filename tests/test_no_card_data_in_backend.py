"""Guards the explicit requirement that the application backend knows
nothing about the company card: no card-related field name anywhere in the
GE->TPL integration source, the admin routes/templates, or the Order/TPL
issuance persistence layer. Card autofill is handled entirely by a private,
operator-local browser extension (browser-extension/bog-card-autofill) that
never talks to this backend -- see tests/test_bog_extension_static.py for
its own guarantees, and the delivery report's SECURITY BOUNDARY section for
the full architecture.

This is a static source scan, not a runtime test -- it locks in the
invariant so a future change can't accidentally introduce a card field
without this test failing.
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_FORBIDDEN_TERMS = ("pan", "cvc", "cvv", "csc", "cardnumber", "card_number", "expirymonth", "expiryyear")
_FORBIDDEN_TERM_RE = {term: re.compile(rf"\b{re.escape(term)}\b") for term in _FORBIDDEN_TERMS}

_FILES_TO_SCAN = [
    "app/integrations/tpl_ge/client.py",
    "app/integrations/tpl_ge/service.py",
    "app/integrations/tpl_ge/repository.py",
    "app/integrations/tpl_ge/models.py",
    "app/integrations/tpl_ge/errors.py",
    "app/web/admin_routes.py",
    "app/web/templates/admin_orders.html",
]

# app/db.py and app/settings.py are deliberately NOT in the general scan:
# app/db.py's own top-level comment legitimately documents "never store
# card/CVC/..." as a protective reminder (checked structurally instead, see
# test_insurance_tpl_issuance_schema_has_no_card_columns below), and
# app/settings.py's PaymentSettings.card_number is a pre-existing, unrelated,
# intentionally-public field -- the RUB bank-transfer card number shown to
# CUSTOMERS on the manual-payment page, nothing to do with the company's
# TPL-purchase card or this task's security boundary.


def test_no_card_field_names_anywhere_in_the_backend():
    # Word-boundary matching -- a plain substring check would false-positive
    # on "pan" inside ordinary words like "company"/"expand".
    for relative_path in _FILES_TO_SCAN:
        text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8").lower()
        for term, pattern in _FORBIDDEN_TERM_RE.items():
            assert not pattern.search(text), f"found forbidden card-related term {term!r} in {relative_path}"


def test_no_long_digit_runs_in_tpl_ge_source():
    """A crude but useful second guard: no 12-19 digit run (the shape of a
    real PAN) anywhere in the integration's own source files."""
    digit_run_pattern = re.compile(r"\d{12,19}")
    for relative_path in _FILES_TO_SCAN:
        text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert not digit_run_pattern.search(text), f"found a long digit run in {relative_path}"


def test_insurance_tpl_issuance_schema_has_no_card_columns():
    """The persisted issuance table's own column list, read directly out of
    app.db.SCHEMA, must never include a card-shaped column."""
    from app.db import SCHEMA

    start = SCHEMA.index("CREATE TABLE IF NOT EXISTS insurance_tpl_issuance")
    end = SCHEMA.index(";", start)
    table_definition = SCHEMA[start:end].lower()
    for term, pattern in _FORBIDDEN_TERM_RE.items():
        assert not pattern.search(table_definition), f"found forbidden card-related term {term!r} in the schema"


def test_no_logging_calls_exist_in_tpl_ge_module_at_all():
    """Belt-and-braces: confirms there is no logging/print statement in the
    integration module that could ever dump a request/response payload --
    so even if a card field were mistakenly added later, there's still no
    code path that writes it to a log."""
    for relative_path in (
        "app/integrations/tpl_ge/client.py",
        "app/integrations/tpl_ge/service.py",
        "app/integrations/tpl_ge/repository.py",
    ):
        text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "print(" not in text
        assert "logging." not in text
        assert "logger." not in text
