"""Armenia (AM) /date step: default end_date pre-fill.

Business rule: the FIRST time a fresh AM draft reaches /date (no dates
chosen yet), start_date defaults to today (already existing behaviour) and
end_date now defaults to start_date + duration_ranges.AM.passenger_car.
default_duration_days (15, see config/config.yaml), so the customer sees a
real 15-day range and a "Срок: 15 дней" label instead of two blank fields.
Once the customer has actually chosen dates (draft has a real end_date),
revisiting /date must show exactly that choice, never reset it back to the
15-day default -- same "never overwrite a real choice" rule the
FIXED-period (GE/TR) branch already followed before this change.

10-365 day min/max validation (app.web.checkout_routes._parse_duration_range_dates)
is completely untouched by this default -- it only affects what's pre-filled
on GET, never what's accepted on POST.

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
    """AM's duration_ranges config (including default_duration_days) lives
    in the REAL config/config.yaml, not the test fixture -- same fixture
    shape as tests/test_am_linear_pricing.py."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


# ---------------------------------------------------------------------------
# AM: default end_date pre-fill
# ---------------------------------------------------------------------------


def test_am_fresh_date_step_defaults_end_date_to_15_days_after_start(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})

    response = client.get("/date")
    assert response.status_code == 200
    today = today_in_georgia()
    expected_end = today + timedelta(days=15)
    assert f'value="{today.isoformat()}"' in response.text
    assert f'value="{expected_end.isoformat()}"' in response.text
    assert "Срок: 15 дней" in response.text
    assert "от 10 до 365 дней" in response.text  # min/max hint unaffected


def test_am_default_dates_are_submittable_as_is(real_config):
    """The pre-filled 15-day default must itself be a valid, acceptable
    submission (start=today, end=today+15 -- squarely inside 10-365), not
    just a display-only placeholder."""
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    today = today_in_georgia()
    expected_end = today + timedelta(days=15)

    response = client.post(
        "/date",
        data={"start_date": today.isoformat(), "end_date": expected_end.isoformat()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/method"


def test_am_in_progress_draft_keeps_its_own_dates_not_the_default(real_config):
    """Once the customer has already chosen real dates (here: a 30-day
    range, not the 15-day default), navigating back to /date must show
    exactly what they chose -- the default must never clobber a real
    in-progress choice."""
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    # A rejected submission (9 days) does NOT persist to the draft -- use a
    # valid one first to seed real dates, matching how a customer would
    # actually reach this state.
    client.post("/date", data={"start_date": _iso(0), "end_date": _iso(30)}, follow_redirects=False)

    client.get("/category-period")  # simulate navigating back
    response = client.get("/date")
    assert response.status_code == 200
    assert f'value="{_iso(0)}"' in response.text
    assert f'value="{_iso(30)}"' in response.text
    assert "Срок: 30 дней" in response.text
    assert "Срок: 15 дней" not in response.text


def test_am_boundaries_still_enforced_on_submit_regardless_of_default(real_config):
    """The default end_date pre-fill has no bearing on the existing 10-365
    day validation -- a customer who edits the pre-filled end_date outside
    that range is still rejected exactly as before."""
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})

    below_min = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(9)})
    assert below_min.status_code == 422

    above_max = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(366)})
    assert above_max.status_code == 422


# ---------------------------------------------------------------------------
# Regression: GE/TR (FIXED-period) /date behaviour is untouched.
# ---------------------------------------------------------------------------


def test_ge_date_step_unaffected_by_am_default(real_config):
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
    # input, and never shows AM's "Срок: N дней"/min-max hint markup.
    assert 'name="end_date"' not in response.text
    assert "Срок:" not in response.text
    assert "duration-days-text" not in response.text


def test_tr_date_step_unaffected_by_am_default(real_config):
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
    assert "Срок:" not in response.text
