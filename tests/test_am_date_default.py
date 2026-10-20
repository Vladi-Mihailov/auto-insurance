"""Armenia (AM) /date step: defaults now that AM is a FIXED-period product.

Business moved AM from an EXACT DATE RANGE (customer-picked start AND end
date, 10-365 days, 15-day default end_date pre-fill) to fixed 15d/30d/90d
periods on 2026-10-20 -- exactly like Georgia. The AM /date screen is now
the same start-date-only screen GE/TR use: start_date defaults to today in
Georgia, end_date is a read-only display computed from the chosen period
(app.checkout.rules._FIXED_DURATION_DATE_RULES["AM"]), and there is no
editable end_date/"Срок, дней" input any more.
Once the customer has actually chosen a start date, revisiting /date must
show exactly that choice, never reset it back to today -- same "never
overwrite a real choice" rule the GE/TR branch always followed.

All dates are computed relative to today_in_georgia(), never a hardcoded
literal -- same time-bomb lesson as the rest of this test suite.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import get_settings
from app.main import app

_settings = get_settings()

# Own catalog rows -- external_id=22001 continues the per-file numbering
# convention (category external_id=7/"passenger_car" is the one
# deliberately-shared id every file reuses, idempotent via
# ON CONFLICT(external_id)).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=22001, name="ZDATEDEFAULTFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=22001, manufacturer_id=_manufacturer_id, name="ZDATEDEFAULTFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=60)


def _iso(offset_days: int) -> str:
    return (_START + timedelta(days=offset_days)).isoformat()


@pytest.fixture
def real_config(monkeypatch):
    """AM's pricing.AM periods live in the REAL config/config.yaml, not the
    test fixture."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


# ---------------------------------------------------------------------------
# AM: start-date-only screen, end_date computed from the period
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("period_code,duration_days", [("15d", 15), ("30d", 30), ("90d", 90)])
def test_am_fresh_date_step_defaults_start_to_today_and_computes_end(real_config, period_code, duration_days):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": period_code})

    response = client.get("/date")
    assert response.status_code == 200
    today = today_in_georgia()
    expected_end = today + timedelta(days=duration_days)
    assert f'value="{today.isoformat()}"' in response.text
    assert expected_end.strftime("%d.%m.%Y") in response.text  # read-only computed display
    assert 'name="end_date"' not in response.text
    assert 'id="duration_days"' not in response.text
    assert "Срок, дней" not in response.text
    assert "от 10 до 365 дней" not in response.text


def test_am_default_start_date_is_submittable_as_is(real_config):
    from app.sessions.repository import get_draft
    from app.deps import SESSION_COOKIE_NAME

    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    today = today_in_georgia()

    response = client.post("/date", data={"start_date": today.isoformat()}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/method"

    conn = get_connection(_settings.app.db_file)
    try:
        draft = get_draft(conn, client.cookies.get(SESSION_COOKIE_NAME))
    finally:
        conn.close()
    assert draft["start_date"] == today.isoformat()
    assert draft["end_date"] == (today + timedelta(days=15)).isoformat()


def test_am_in_progress_draft_keeps_its_own_start_date_not_the_default(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)

    client.get("/category-period")  # simulate navigating back
    response = client.get("/date")
    assert response.status_code == 200
    assert f'value="{_iso(0)}"' in response.text
    assert (_START + timedelta(days=30)).strftime("%d.%m.%Y") in response.text


# ---------------------------------------------------------------------------
# Regression: GE/TR (FIXED-period) /date behaviour is untouched.
# ---------------------------------------------------------------------------


def test_ge_date_step_unaffected_by_am_change(real_config):
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})

    response = client.get("/date")
    assert response.status_code == 200
    today = today_in_georgia()
    expected_end = today + timedelta(days=15)
    assert f'value="{today.isoformat()}"' in response.text
    assert expected_end.strftime("%d.%m.%Y") in response.text
    # GE's own end date stays a read-only computed display, not an editable
    # input, and never shows the (now unused) duration-range markup.
    assert 'name="end_date"' not in response.text
    assert 'id="duration_days"' not in response.text
    assert "Срок, дней" not in response.text


def test_tr_date_step_unaffected_by_am_change(real_config):
    client = TestClient(app)
    _start(client, "TR")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})

    response = client.get("/date")
    assert response.status_code == 200
    today = today_in_georgia()
    expected_end = today + timedelta(days=30)
    assert f'value="{today.isoformat()}"' in response.text
    assert expected_end.strftime("%d.%m.%Y") in response.text
    assert 'name="end_date"' not in response.text
    assert 'id="duration_days"' not in response.text
