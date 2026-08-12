"""Sets test-only environment variables BEFORE any test module imports app.main.

This must happen at conftest import time (not inside a fixture) because
pytest imports conftest.py before collecting sibling test modules, while a
fixture only runs once a test actually executes — by then `import app.main`
in a test file would already have triggered get_settings()/init_db() against
the real project .env/data directory.
"""

import os
import tempfile
from pathlib import Path

_tmp_dir = Path(tempfile.mkdtemp(prefix="auto_insurance_test_"))
os.environ.setdefault("INSURANCE_DB_FILE", str(_tmp_dir / "insurance.db"))
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("PAYMENT_BANK_NAME", "Test Bank")
os.environ.setdefault("PAYMENT_CARD_NUMBER", "0000 0000 0000 0000")
os.environ.setdefault("PAYMENT_CARD_HOLDER", "TEST HOLDER")
os.environ.setdefault("INSURANCE_CONFIG_FILE", "tests/fixtures/test_config.yaml")
