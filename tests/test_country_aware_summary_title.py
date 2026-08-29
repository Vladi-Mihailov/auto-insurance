"""Country-aware summary page title (cosmetic fix).

The summary page's <h1> title was hardcoded to "ОСАГО Грузии" regardless of
the order's actual country -- discovered during Armenia's public-launch
production smoke test. Fixed in app/web/templates/summary.html by mapping
order.country_code (already passed into this template via the `order`
context variable -- no new country-state introduced) to the right
genitive-case title: GE is the default, AM/TR are looked up.

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
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()

# Own catalog rows -- external_id=21001 continues the per-file numbering
# convention (category external_id=7/"passenger_car" is the one
# deliberately-shared id every file reuses, idempotent via
# ON CONFLICT(external_id)).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=21001, name="ZTITLEFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=21001, manufacturer_id=_manufacturer_id, name="ZTITLEFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=60)


def _iso(offset_days: int) -> str:
    return (_START + timedelta(days=offset_days)).isoformat()


@pytest.fixture
def real_config(monkeypatch):
    """AM/TR pricing lives in the REAL config/config.yaml, not the test
    fixture -- same fixture shape as tests/test_am_linear_pricing.py."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


def _vehicle_data(reg, **overrides):
    data = {
        "registration_number": reg,
        "identifier_type": "vin",
        "identifier": "JT123456789012345",
        "manufacturer_id": str(_manufacturer_id),
        "model_id": str(_model_id),
    }
    data.update(overrides)
    return data


def test_ge_summary_title_is_osago_gruzii():
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": today_in_georgia().isoformat()})
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_data("TITLEGE1"))
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@title_ge"), follow_redirects=False
    )
    resume_token = response.headers["location"].split("/")[2]

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "ОСАГО Грузии" in summary.text


def test_am_summary_title_is_osago_armenii(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    client.post("/date", data={"start_date": _iso(0), "end_date": _iso(30)})
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_data("TITLEAM1", engine_power="150"))
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@title_am"), follow_redirects=False
    )
    resume_token = response.headers["location"].split("/")[2]

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "ОСАГО Армении" in summary.text
    assert "ОСАГО Грузии" not in summary.text


def test_tr_summary_title_is_osago_turcii(real_config):
    client = TestClient(app)
    _start(client, "TR")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client.post("/date", data={"start_date": _iso(0)})
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_data("TITLETR1", engine_power="150", model_year="2020"))
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@title_tr", date_of_birth="1990-05-20"),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "ОСАГО Турции" in summary.text
    assert "ОСАГО Грузии" not in summary.text
