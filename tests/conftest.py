"""Sets test-only environment variables BEFORE any test module imports app.main.

This must happen at conftest import time (not inside a fixture) because
pytest imports conftest.py before collecting sibling test modules, while a
fixture only runs once a test actually executes — by then `import app.main`
in a test file would already have triggered get_settings()/init_db() against
the real project .env/data directory.

Test isolation from real credentials/services is enforced three ways, all
here, all unconditional:

1. INSURANCE_LOAD_DOTENV=0 -- app.settings.load_settings never reads the
   developer's real .env at all during a test run.
2. Every credential/external-service variable in _SENSITIVE_ENV_VARS is
   REMOVED from os.environ (not just defaulted), so one inherited from the
   developer's own shell can't leak in either. Tests that need one set a
   fake value themselves via monkeypatch.setenv.
3. _block_external_network (below) refuses any socket connection/DNS lookup
   to a non-loopback host, so a test that forgets to stub OpenAI/Telegram/
   tpl.ge fails loudly instead of silently calling a real service. An
   integration test may opt out only via INSURANCE_ALLOW_NETWORK_TESTS=1,
   set explicitly by whoever runs it -- no test in this suite does.
"""

import ipaddress
import os
import socket
import tempfile
from pathlib import Path

import pytest

_tmp_dir = Path(tempfile.mkdtemp(prefix="auto_insurance_test_"))

os.environ["INSURANCE_LOAD_DOTENV"] = "0"
# Forced, not setdefault: a shell-level INSURANCE_DB_FILE pointing at the
# real data/insurance.db must never be honoured by a test run.
os.environ["INSURANCE_DB_FILE"] = str(_tmp_dir / "insurance.db")
os.environ["INSURANCE_CONFIG_FILE"] = "tests/fixtures/test_config.yaml"

_SENSITIVE_ENV_VARS = (
    "OPENAI_API_KEY",
    "OCR_VISION_MODEL",
    "OCR_ORIENTATION_DETECTOR",
    "OCR_TESSERACT_CMD",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "TPL_GE_STATIC_VISITOR_ID",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_PHONE",
    "TELEGRAM_OPERATOR_CHAT_ID",
    "TELEGRAM_OPERATOR_SESSION_PATH",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_KEY",
    "TELEGRAM_BOT_MANAGER_IDS",
    "TELEGRAM_PAYMENT_BANK_NAME",
    "TELEGRAM_PAYMENT_PHONE_NUMBER",
    "TELEGRAM_PAYMENT_RECIPIENT",
    "TELEGRAM_PAYMENT_INSTRUCTIONS",
    "CONTACT_MAX_URL",
    "CONTACT_TELEGRAM_URL",
    "CONTACT_VK_URL",
)
for _name in _SENSITIVE_ENV_VARS:
    os.environ.pop(_name, None)

os.environ["COOKIE_SECURE"] = "false"
os.environ["APP_SECRET_KEY"] = "test-secret"
os.environ["PAYMENT_BANK_NAME"] = "Test Bank"
os.environ["PAYMENT_CARD_NUMBER"] = "0000 0000 0000 0000"
os.environ["PAYMENT_CARD_HOLDER"] = "TEST HOLDER"
# Explicitly blank -- "not configured" test scenarios depend on these being
# absent (see PAYMENT_QR_IMAGE_URL/PAYMENT_TRANSFER_URL in app.settings).
os.environ["PAYMENT_QR_IMAGE_URL"] = ""
os.environ["PAYMENT_TRANSFER_URL"] = ""


_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", ""}


def _is_loopback_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ExternalNetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True, scope="session")
def _block_external_network():
    if os.getenv("INSURANCE_ALLOW_NETWORK_TESTS") == "1":
        yield
        return

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback_host(host):
            raise ExternalNetworkBlocked(f"test attempted DNS lookup of external host {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _check_address(address):
        host = address[0] if isinstance(address, tuple) else None
        if isinstance(address, tuple) and not _is_loopback_host(host):
            raise ExternalNetworkBlocked(f"test attempted connection to external host {host!r}")

    def guarded_connect(self, address):
        _check_address(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check_address(address)
        return real_connect_ex(self, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
