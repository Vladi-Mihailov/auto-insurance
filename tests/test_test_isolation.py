"""Locks in tests/conftest.py's isolation guarantees: no real .env
credential reaches a test, no test can reach an external host, and no
checkout test depends on a hard-coded start date that will silently expire."""

import os
import re
import socket
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.dates.rules import today_in_georgia
from app.deps import PROJECT_ROOT
from app.settings import load_settings

from conftest import _SENSITIVE_ENV_VARS, ExternalNetworkBlocked

_TESTS_DIR = Path(__file__).resolve().parent


def test_dotenv_loading_is_disabled_for_tests():
    assert os.environ["INSURANCE_LOAD_DOTENV"] == "0"


@pytest.mark.parametrize("name", _SENSITIVE_ENV_VARS)
def test_sensitive_env_var_is_absent(name):
    assert name not in os.environ


def test_real_dotenv_file_is_not_loaded_even_when_present(tmp_path, monkeypatch):
    """A project root with a .env containing a (fake) key must still yield
    settings without it -- load_settings honours INSURANCE_LOAD_DOTENV=0."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("pricing: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-should-never-load\n", encoding="utf-8")
    monkeypatch.setenv("INSURANCE_CONFIG_FILE", "config/config.yaml")
    settings = load_settings(tmp_path)
    assert settings.ocr.openai_api_key is None
    assert "OPENAI_API_KEY" not in os.environ


def test_test_db_is_not_the_real_project_db():
    real_db = (PROJECT_ROOT / "data" / "insurance.db").resolve()
    assert Path(os.environ["INSURANCE_DB_FILE"]).resolve() != real_db


@pytest.mark.parametrize("host", ["api.openai.com", "api.telegram.org", "tpl.ge"])
def test_external_dns_is_blocked(host):
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo(host, 443)


def test_external_connect_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ExternalNetworkBlocked):
            sock.connect(("149.154.167.220", 443))  # a Telegram DC address
    finally:
        sock.close()


def test_loopback_is_still_allowed():
    """asyncio's Windows self-pipe uses a loopback socketpair -- must work."""
    a, b = socket.socketpair()
    a.close()
    b.close()


_LITERAL_START_DATE_RE = re.compile(r'"start_date"\s*:\s*"(\d{4}-\d{2}-\d{2})"')
# Files whose start_date literals only ever feed pure functions (draft dicts
# for step_nav, date-math inputs) -- never validated against "today", so a
# past date there is fine and intentional.
_PURE_FUNCTION_DATE_FILES = {"test_step_nav.py", "test_date_rules.py"}
# Every other literal must stay comfortably in the future; this gives a
# year's warning before one becomes a past date and starts failing the
# server-side "not before today" validation (the failure mode that broke
# test_payment.py/test_policyholder_contacts.py).
_MIN_MARGIN = timedelta(days=365)


def test_no_checkout_start_date_literal_is_close_to_expiring():
    today = today_in_georgia()
    stale = []
    for path in _TESTS_DIR.glob("test_*.py"):
        if path.name in _PURE_FUNCTION_DATE_FILES:
            continue
        for match in _LITERAL_START_DATE_RE.finditer(path.read_text(encoding="utf-8")):
            if date.fromisoformat(match.group(1)) < today + _MIN_MARGIN:
                stale.append(f"{path.name}: {match.group(1)}")
    assert not stale, "Use today_in_georgia() + timedelta(...) instead of: " + ", ".join(stale)
