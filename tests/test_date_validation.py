"""Date-step UX/validation regression tests.

Covers: immediate end-date recalculation (backend source of truth, same
GeorgiaDateRule the /api/date-preview endpoint already used), and the new
past-date guard (today allowed, yesterday rejected server-side -- the HTML
min= attribute is a UX nicety only, never trusted alone).

"passenger_car" here is re-seeded with the same external_id=7 already used
by test_routes_smoke.py -- upsert_category is a true idempotent upsert (see
app.catalog.repository), so this is safe regardless of test collection
order and doesn't depend on that other module having run first.
"""

from datetime import timedelta

from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import get_settings
from app.main import app
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
# external_id chosen to not collide with other test files' ranges (see
# tests/test_special_vehicle.py's module docstring for the full list).
_manufacturer_id = upsert_manufacturer(_conn, external_id=11001, name="ZDATEFICTIONALMAKE", is_popular=True)
_model_id = upsert_model(_conn, external_id=11001, manufacturer_id=_manufacturer_id, name="ZDATEFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


def _create_full_order(client_):
    _reach_date_step(client_, "15d")
    today = today_in_georgia()
    client_.post("/date", data={"start_date": today.isoformat()})
    client_.post("/method", data={"choice": "manual"})
    client_.post(
        "/vehicle",
        data={
            "registration_number": "DT001AA",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000401",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@ivan"),
        follow_redirects=False,
    )
    return response.headers["location"].split("/")[2]


def _reach_date_step(client_, period_code="15d"):
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": period_code})


def test_date_screen_min_attribute_is_todays_georgia_date():
    client_ = TestClient(app)
    _reach_date_step(client_)
    response = client_.get("/date")
    assert response.status_code == 200
    assert f'min="{today_in_georgia().isoformat()}"' in response.text


def test_today_is_accepted_as_start_date():
    client_ = TestClient(app)
    _reach_date_step(client_)
    today = today_in_georgia()
    response = client_.post("/date", data={"start_date": today.isoformat()}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/method"


def test_yesterday_is_rejected_server_side():
    client_ = TestClient(app)
    _reach_date_step(client_)
    yesterday = today_in_georgia() - timedelta(days=1)
    response = client_.post("/date", data={"start_date": yesterday.isoformat()})
    assert response.status_code == 422
    assert "не может быть раньше сегодняшнего дня" in response.text


def test_future_date_is_accepted():
    client_ = TestClient(app)
    _reach_date_step(client_)
    future = today_in_georgia() + timedelta(days=10)
    response = client_.post("/date", data={"start_date": future.isoformat()}, follow_redirects=False)
    assert response.status_code == 303


def test_end_date_correct_for_15d():
    client_ = TestClient(app)
    _reach_date_step(client_, "15d")
    today = today_in_georgia()
    response = client_.get("/api/date-preview", params={"start": today.isoformat()})
    assert response.json() == {"end_date": (today + timedelta(days=15)).isoformat()}


def test_end_date_correct_for_30d():
    client_ = TestClient(app)
    _reach_date_step(client_, "30d")
    today = today_in_georgia()
    response = client_.get("/api/date-preview", params={"start": today.isoformat()})
    assert response.json() == {"end_date": (today + timedelta(days=30)).isoformat()}


def test_end_date_correct_for_90d():
    client_ = TestClient(app)
    _reach_date_step(client_, "90d")
    today = today_in_georgia()
    response = client_.get("/api/date-preview", params={"start": today.isoformat()})
    assert response.json() == {"end_date": (today + timedelta(days=90)).isoformat()}


def test_changing_start_date_produces_a_correct_new_end_date():
    """The frontend calls /api/date-preview on every change of #start_date
    (see app/web/static/js/main.js) -- this confirms the backend endpoint
    it depends on recomputes correctly for a second, different start date
    within the same session, not just the first one."""
    client_ = TestClient(app)
    _reach_date_step(client_, "30d")
    today = today_in_georgia()

    first = client_.get("/api/date-preview", params={"start": today.isoformat()})
    assert first.json() == {"end_date": (today + timedelta(days=30)).isoformat()}

    later_start = today + timedelta(days=5)
    second = client_.get("/api/date-preview", params={"start": later_start.isoformat()})
    assert second.json() == {"end_date": (later_start + timedelta(days=30)).isoformat()}


def test_fresh_date_step_defaults_start_date_to_georgia_today():
    client_ = TestClient(app)
    _reach_date_step(client_)
    response = client_.get("/date")
    assert response.status_code == 200
    today = today_in_georgia()
    assert f'value="{today.isoformat()}"' in response.text


def test_fresh_date_step_end_date_preview_matches_defaulted_today():
    client_ = TestClient(app)
    _reach_date_step(client_, "30d")
    response = client_.get("/date")
    assert response.status_code == 200
    today = today_in_georgia()
    expected_end = today + timedelta(days=30)
    assert expected_end.strftime("%d.%m.%Y") in response.text
    assert "Рассчитается автоматически" not in response.text


def test_chosen_future_date_survives_back_and_forward_navigation():
    """The today-default is only for a draft that has no start_date at
    all -- once the user picked a real (even future) date, revisiting
    /date must keep showing that choice, never silently reset it back to
    today."""
    client_ = TestClient(app)
    _reach_date_step(client_)
    future = today_in_georgia() + timedelta(days=10)
    client_.post("/date", data={"start_date": future.isoformat()})

    client_.get("/category-period")  # simulate navigating back
    response = client_.get("/date")
    assert response.status_code == 200
    assert f'value="{future.isoformat()}"' in response.text


def test_edit_date_also_rejects_a_past_date():
    """Same past-date guard applies to the post-order /o/{token}/edit-date
    route, not just the pre-order /date step."""
    client_ = TestClient(app)
    resume_token = _create_full_order(client_)

    yesterday = today_in_georgia() - timedelta(days=1)
    response = client_.post(f"/o/{resume_token}/edit-date", data={"start_date": yesterday.isoformat()})
    assert response.status_code == 422
    assert "не может быть раньше сегодняшнего дня" in response.text

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.start_date == today_in_georgia()  # unchanged
