import re

import pytest
from fastapi.testclient import TestClient

from app.catalog import repository as catalog_repo
from app.catalog.repository import upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import get_settings
from app.main import app

client = TestClient(app)

# Seed a minimal, realistic catalog into the same (test-only) DB app.main just
# initialized — mirrors what `python -m app.catalog.sync` would produce, but
# without hitting the real network in tests. See tests/test_catalog_sync.py
# for sync-logic correctness on its own.
_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_bmw_id = upsert_manufacturer(_conn, external_id=12, name="BMW", is_popular=True)
_model_id = upsert_model(_conn, external_id=361, manufacturer_id=_bmw_id, name="730 LD")
_other_manufacturer_id = upsert_manufacturer(_conn, external_id=7, name="AUDI", is_popular=False)
_other_model_id = upsert_model(_conn, external_id=99, manufacturer_id=_other_manufacturer_id, name="A4")
_inactive_manufacturer_id = upsert_manufacturer(_conn, external_id=999, name="DEFUNCT MOTORS", is_popular=False)
_inactive_model_id = upsert_model(_conn, external_id=1, manufacturer_id=_bmw_id, name="INACTIVE MODEL")
_bmw_other_model_id = upsert_model(_conn, external_id=24376, manufacturer_id=_bmw_id, name="Other")
# Mark BMW/AUDI's models as already synced — otherwise /api/vehicle-models
# would see models_never_synced and try an on-demand sync against the real
# tpl.ge network from inside the test suite (see app.catalog.sync).
catalog_repo.mark_models_synced(_conn, _bmw_id)
catalog_repo.mark_models_synced(_conn, _other_manufacturer_id)
_conn.execute("UPDATE insurance_manufacturers SET active = 0 WHERE id = ?", (_inactive_manufacturer_id,))
_conn.execute("UPDATE insurance_vehicle_models SET active = 0 WHERE id = ?", (_inactive_model_id,))
_conn.commit()
_conn.close()


def test_landing_page_shows_offer():
    response = client.get("/")
    assert response.status_code == 200
    assert "1 500" in response.text  # from tests/fixtures/test_config.yaml
    assert "Грузию" in response.text
    assert "/start" in response.text


def test_unknown_resume_token_is_404():
    response = client.get("/o/does-not-exist")
    assert response.status_code == 404


def test_api_manufacturers_search():
    response = client.get("/api/manufacturers", params={"q": "bmw"})
    assert response.status_code == 200
    names = [m["name"] for m in response.json()]
    assert names == ["BMW"]


def test_api_vehicle_models_scoped_to_manufacturer():
    response = client.get("/api/vehicle-models", params={"manufacturer_id": _bmw_id})
    assert response.status_code == 200
    body = response.json()
    assert body["synced"] is True
    assert {m["id"]: m["name"] for m in body["models"]} == {_model_id: "730 LD", _bmw_other_model_id: "Other"}


def test_api_vehicle_models_unknown_manufacturer_id_is_empty_not_an_error():
    response = client.get("/api/vehicle-models", params={"manufacturer_id": 999999})
    assert response.status_code == 200
    assert response.json() == {"models": [], "synced": True}


def test_api_vehicle_models_inactive_manufacturer_is_treated_as_unknown():
    response = client.get("/api/vehicle-models", params={"manufacturer_id": _inactive_manufacturer_id})
    assert response.status_code == 200
    assert response.json() == {"models": [], "synced": True}
    # not "synced": False — an inactive manufacturer isn't "never synced",
    # it's just filtered out by get_manufacturer(active=1), same as unknown


def _create_manufacturer_row(external_id: int, name: str) -> int:
    conn = get_connection(_settings.app.db_file)
    try:
        manufacturer_id = upsert_manufacturer(conn, external_id=external_id, name=name, is_popular=False)
        conn.commit()
    finally:
        conn.close()
    return manufacturer_id


def _fake_on_demand_sync(conn, manufacturer):
    from app.catalog.repository import mark_models_synced, upsert_model

    upsert_model(conn, external_id=1, manufacturer_id=manufacturer.id, name="GIULIA")
    mark_models_synced(conn, manufacturer.id)
    conn.commit()
    return True


def test_api_vehicle_models_never_synced_manufacturer_triggers_on_demand_sync(monkeypatch):
    """The core guarantee item 1 asks for: a manufacturer whose models were
    never synced must not just come back empty — the backend (never the
    browser) fetches them on the spot instead."""
    from app.web import checkout_routes

    new_manufacturer_id = _create_manufacturer_row(external_id=3, name="ALFA ROMEO")
    monkeypatch.setattr(checkout_routes, "sync_models_on_demand", _fake_on_demand_sync)

    response = client.get("/api/vehicle-models", params={"manufacturer_id": new_manufacturer_id})
    assert response.status_code == 200
    body = response.json()
    assert body["synced"] is True
    assert {m["name"] for m in body["models"]} == {"GIULIA"}


def test_api_vehicle_models_on_demand_sync_failure_reports_synced_false(monkeypatch):
    from app.web import checkout_routes

    new_manufacturer_id = _create_manufacturer_row(external_id=4, name="ARO")
    monkeypatch.setattr(checkout_routes, "sync_models_on_demand", lambda conn, manufacturer: False)

    response = client.get("/api/vehicle-models", params={"manufacturer_id": new_manufacturer_id})
    assert response.status_code == 200
    assert response.json() == {"models": [], "synced": False}


def test_api_periods_for_category():
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    assert response.status_code == 200
    codes = [p["code"] for p in response.json()]
    assert codes == ["15d", "30d", "90d", "1y"]


def test_vehicle_step_rejects_model_from_a_different_manufacturer():
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})
    fresh_client.post("/method", data={"choice": "manual"})

    response = fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_other_model_id),  # belongs to AUDI, not BMW
        },
    )
    assert response.status_code == 422
    assert "модель" in response.text.lower()


def test_vehicle_step_rejects_inactive_manufacturer():
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})
    fresh_client.post("/method", data={"choice": "manual"})

    response = fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_inactive_manufacturer_id),
            "model_id": "1",
        },
    )
    assert response.status_code == 422
    assert "производит" in response.text.lower()


def test_vehicle_step_rejects_inactive_model():
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})
    fresh_client.post("/method", data={"choice": "manual"})

    response = fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_inactive_model_id),
        },
    )
    assert response.status_code == 422
    assert "модель" in response.text.lower()


def test_vehicle_step_rejects_arbitrary_model_id():
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})
    fresh_client.post("/method", data={"choice": "manual"})

    response = fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": "999999999",  # does not exist at all
        },
    )
    assert response.status_code == 422
    assert "модель" in response.text.lower()


def test_vehicle_step_accepts_other_model_through_the_same_validation_as_any_model():
    """"Other" is a regular synced row per manufacturer (its own external_id
    — see app.catalog.client) — it must go through the exact same
    manufacturer-match/active checks as any real model, not a bypass."""
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})
    fresh_client.post("/method", data={"choice": "manual"})

    response = fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_bmw_other_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"


def test_documents_soon_manual_fallback_continues_the_same_draft():
    """Regression test: choosing "Загрузить документы" then using the
    "Ввести данные вручную" fallback on /documents-soon must land on
    /vehicle with the SAME draft, not dead-end back at /method. The
    fallback posts choice=manual to the existing /method handler (so it
    explicitly sets data_entry_method="manual"), not a bare link -- see
    checkout_routes.get_vehicle's docstring for why a bare link plus a
    lenient GET-time default isn't enough once documents is a real path."""
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})

    response = fresh_client.post("/method", data={"choice": "documents"}, follow_redirects=False)
    assert response.headers["location"] == "/documents-soon"

    response = fresh_client.get("/documents-soon")
    assert response.status_code == 200
    assert 'action="/method"' in response.text
    assert 'value="manual"' in response.text  # the fallback button itself

    # Using that fallback (POST choice=manual to /method) must land on
    # /vehicle, not bounce back to /method, and must explicitly flip
    # data_entry_method to "manual".
    response = fresh_client.post("/method", data={"choice": "manual"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    response = fresh_client.get("/vehicle")
    assert response.status_code == 200

    response = fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"  # completed the SAME draft, not a dead end


def test_double_submit_policyholder_does_not_create_a_duplicate_order():
    """Regression test: the draft used to survive order creation, so a
    resubmitted POST /policyholder (browser back + resubmit, double-click)
    created a second order from the same data instead of failing safely."""
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})
    fresh_client.post("/method", data={"choice": "manual"})
    fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_model_id),
        },
    )
    policyholder_data = {"full_name": "Ivanov Ivan", "contact_email": "ivan@example.com", "contact_telegram": "@ivan"}

    first = fresh_client.post("/policyholder", data=policyholder_data, follow_redirects=False)
    assert first.status_code == 303
    first_location = first.headers["location"]

    # Resubmit the exact same form (draft should now be cleared).
    second = fresh_client.post("/policyholder", data=policyholder_data, follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == "/vehicle"  # guard now fails instead of creating order #2
    assert second.headers["location"] != first_location


def test_back_to_vehicle_then_forward_again_does_not_duplicate_or_corrupt_draft():
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})
    fresh_client.post("/method", data={"choice": "manual"})
    fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_model_id),
        },
    )

    # Back to /vehicle — GET must still work and reflect the saved draft.
    back = fresh_client.get("/vehicle")
    assert back.status_code == 200
    assert "A123AA777" in back.text

    # Forward again to /policyholder, then create the order — exactly once.
    response = fresh_client.get("/policyholder")
    assert response.status_code == 200

    created = fresh_client.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "contact_email": "ivan@example.com", "contact_telegram": "@ivan"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    resume_token = created.headers["location"].split("/")[2]

    # refresh after order creation must not create a second order either.
    resumed = fresh_client.get(f"/o/{resume_token}")
    assert resumed.status_code in (303, 200)


def test_vehicle_step_rejects_invalid_identifier():
    fresh_client = TestClient(app)
    fresh_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    fresh_client.post("/date", data={"start_date": "2026-08-15"})
    fresh_client.post("/method", data={"choice": "manual"})

    response = fresh_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "bad vin!!",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_model_id),
        },
    )
    assert response.status_code == 422
    assert "VIN" in response.text


def test_full_happy_path_reaches_payment_screen():
    happy_client = TestClient(app)

    response = happy_client.get("/start", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/category-period"

    response = happy_client.get("/category-period")
    assert response.status_code == 200

    response = happy_client.post(
        "/category-period",
        data={"category_code": "passenger_car", "period_code": "15d"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"

    response = happy_client.get("/date")
    assert response.status_code == 200

    response = happy_client.post("/date", data={"start_date": "2026-08-15"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/method"

    response = happy_client.get("/method")
    assert response.status_code == 200

    response = happy_client.post("/method", data={"choice": "manual"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"

    response = happy_client.get("/vehicle")
    assert response.status_code == 200

    response = happy_client.post(
        "/vehicle",
        data={
            "registration_number": "a123aa777",
            "identifier_type": "vin",
            "identifier": "jt123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/policyholder"

    response = happy_client.get("/policyholder")
    assert response.status_code == 200

    response = happy_client.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "contact_email": "ivan@example.com", "contact_telegram": "@ivan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/o/")
    assert location.endswith("/summary")
    resume_token = location.split("/")[2]

    response = happy_client.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    assert "30.08.2026" in response.text  # 15d from 15.08 -> 30.08, per GeorgiaDateRule
    assert "1 500" in response.text
    assert "BMW" in response.text
    assert "730 LD" in response.text

    response = happy_client.post(f"/o/{resume_token}/summary", data={"action": "pay"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/o/{resume_token}/payment"

    response = happy_client.get(f"/o/{resume_token}/payment")
    assert response.status_code == 200
    assert "К оплате" in response.text

    response = happy_client.get(f"/o/{resume_token}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/o/{resume_token}/payment"


def test_edit_vehicle_updates_an_existing_order():
    edit_client = TestClient(app)
    edit_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    edit_client.post("/date", data={"start_date": "2026-08-15"})
    edit_client.post("/method", data={"choice": "manual"})
    edit_client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_model_id),
        },
    )
    response = edit_client.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "contact_email": "ivan@example.com", "contact_telegram": "@ivan"},
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]

    response = edit_client.get(f"/o/{resume_token}/edit-vehicle")
    assert response.status_code == 200
    assert "BMW" in response.text

    response = edit_client.post(
        f"/o/{resume_token}/edit-vehicle",
        data={
            "registration_number": "B456BB777",
            "identifier_type": "chassis",
            "identifier": "CHS999999",
            "manufacturer_id": str(_other_manufacturer_id),
            "model_id": str(_other_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/o/{resume_token}/summary"

    response = edit_client.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    assert "AUDI" in response.text
    assert "B456BB777" in response.text
    assert "CHS999999" in response.text
    assert "Номер шасси" in response.text


def test_date_preview_endpoint_uses_the_draft_period_pre_order():
    happy_client = TestClient(app)
    happy_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "1y"})

    response = happy_client.get("/api/date-preview", params={"start": "2026-01-31"})
    assert response.status_code == 200
    assert response.json() == {"end_date": "2027-01-31"}


def test_back_then_forward_keeps_earlier_draft_values():
    """Regression guard for the pre-order draft merge — selecting category
    then dates then going back to re-check category-period must not wipe
    the dates already chosen."""
    happy_client = TestClient(app)
    happy_client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    happy_client.post("/date", data={"start_date": "2026-08-15"})

    response = happy_client.get("/category-period")
    assert response.status_code == 200
    assert 'value="passenger_car"' in response.text

    response = happy_client.get("/date")
    assert response.status_code == 200
    assert 'value="2026-08-15"' in response.text


def test_forward_revisit_of_a_previously_completed_step_shows_saved_values():
    """"возврат вперёд на previously completed step" -- once vehicle details
    are saved, GETting /vehicle again (e.g. via the progress nav) must show
    them, not a blank form."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})
    client_.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_model_id),
        },
    )

    # Back to /date, then forward again straight to /vehicle (simulates
    # clicking "Данные авто" in the progress nav rather than /method's button).
    client_.get("/date")
    response = client_.get("/vehicle")
    assert response.status_code == 200
    assert "BMW" in response.text
    assert "730 LD" in response.text
    assert 'value="A123AA777"' in response.text


def test_changing_period_recomputes_end_date_for_the_new_period():
    """Dependency invalidation: a start_date picked under 30d must not
    silently keep the 30d end_date after the user goes back and switches to
    90d."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client_.post("/date", data={"start_date": "2026-08-15"})

    response = client_.get("/api/date-preview", params={"start": "2026-08-15"})
    assert response.json() == {"end_date": "2026-09-14"}  # 30d

    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "90d"})

    response = client_.get("/date")
    assert response.status_code == 200
    assert "13.11.2026" in response.text  # 15.08 + 90d, recomputed for the new period
    assert "14.09.2026" not in response.text  # stale 30d end_date must be gone

    response = client_.get("/api/date-preview", params={"start": "2026-08-15"})
    assert response.json() == {"end_date": "2026-11-13"}


def test_cannot_url_jump_past_a_required_step_on_a_fresh_session():
    """"нельзя URL-ом перескочить обязательный step" -- every pre-order step
    redirects back to the last completed step when its own required draft
    keys are missing, for a session that has done nothing yet."""
    client_ = TestClient(app)

    response = client_.get("/date", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/category-period"

    response = client_.get("/method", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/date"

    response = client_.get("/vehicle", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/date"

    response = client_.get("/policyholder", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/vehicle"


def test_progress_nav_only_links_completed_steps():
    """Template-level check that base.html actually renders the steps a
    route computed: on /method, steps 1+2 (done) must be links and steps
    5+6 (future) must not be."""
    client_ = TestClient(app)
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client_.post("/date", data={"start_date": "2026-08-15"})

    response = client_.get("/method")
    assert response.status_code == 200
    assert '<a href="/category-period" class="progress__num">' in response.text
    assert '<a href="/date" class="progress__num">' in response.text
    assert '<a href="/policyholder"' not in response.text


def test_production_pricing_confirmed_for_ge_passenger_car(monkeypatch):
    """Business confirmed updated 15d/30d/90d RUB prices on 2026-08-13 --
    checks the REAL config/config.yaml (not the tests/fixtures/ dummy
    pricing) has them wired correctly, and that 1y no longer exists as a
    period at all (not just unpriced -- we can't sell a 1-year policy)."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    from app.deps import PROJECT_ROOT
    from app.pricing.provider import available_periods
    from app.settings import load_settings

    settings = load_settings(PROJECT_ROOT)
    periods = {p.code: p for p in available_periods(settings, "GE", "passenger_car")}
    assert periods["15d"].price_rub == 1349
    assert periods["30d"].price_rub == 2149
    assert periods["90d"].price_rub == 3649
    assert "1y" not in periods


@pytest.fixture
def real_production_config(monkeypatch):
    """get_settings() is @lru_cache'd in app.deps (deliberately, so it's not
    re-read on every request) -- by the time any test runs, some earlier
    test has already populated that cache from the fixture config. Just
    unsetting the env var has no effect on an already-cached Settings
    object, so the cache has to be cleared too: once now (so the next
    get_settings() call re-reads the real file while the env var is
    unset), and once more after the test (so later tests re-cache the
    fixture config instead of leaking this test's real-config object)."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_category_period_unblocked_with_real_production_prices(real_production_config):
    """The concrete blocker this pricing update fixes: before, every period
    was price_rub: null in the real config and nothing could ever reach
    /date. Uses the real config/config.yaml, not the test fixture."""
    real_client = TestClient(app)
    response = real_client.post(
        "/category-period",
        data={"category_code": "passenger_car", "period_code": "30d"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"


def test_cannot_select_1y_period_since_it_no_longer_exists_against_real_config(real_production_config):
    """1y isn't just unpriced anymore -- it's absent from config entirely
    (we can't sell a 1-year policy), so submitting it hits the "unknown
    period" branch, not the "not priced yet" one."""
    real_client = TestClient(app)
    response = real_client.post(
        "/category-period",
        data={"category_code": "passenger_car", "period_code": "1y"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text


def test_category_period_screen_never_renders_a_1y_card_against_real_config(real_production_config):
    real_client = TestClient(app)
    response = real_client.get("/category-period")
    assert response.status_code == 200
    assert "1 год" not in response.text
    assert 'data-period-code="1y"' not in response.text
    assert "Цена уточняется" not in response.text


def test_api_periods_for_passenger_car_excludes_1y_against_real_config(real_production_config):
    real_client = TestClient(app)
    response = real_client.get("/api/periods", params={"category_code": "passenger_car"})
    assert response.status_code == 200
    codes = [p["code"] for p in response.json()]
    assert codes == ["15d", "30d", "90d"]


def test_category_period_screen_has_no_summary_or_info_block():
    """Sections 3-4 of the UX cleanup: the extra "Выбранный транспорт /
    Срок страхования / Итого к оплате" block and the RUB disclaimer plashka
    were removed as redundant with the cards themselves."""
    client_ = TestClient(app)
    response = client_.get("/category-period")
    assert response.status_code == 200
    assert "cp-summary" not in response.text
    assert "Выбранный транспорт" not in response.text
    assert "Итого к оплате" not in response.text
    assert "Цены указаны в рублях" not in response.text
    assert "cp-note" not in response.text


def _count_orders() -> int:
    conn = get_connection(_settings.app.db_file)
    try:
        return conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0]
    finally:
        conn.close()


def _create_full_order(client_) -> str:
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2026-08-15"})
    client_.post("/method", data={"choice": "manual"})
    client_.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_bmw_id),
            "model_id": str(_model_id),
        },
    )
    response = client_.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "contact_email": "ivan@example.com", "contact_telegram": "@ivan"},
        follow_redirects=False,
    )
    return response.headers["location"].split("/")[2]


def test_review_screen_has_exactly_one_active_step_and_correct_clickability():
    """Section 1/16: on Проверка, step 6 alone is active-blue; steps
    1/2/4/5 are completed-and-clickable (real edit routes exist); step 3
    is completed but has no href (no edit route by design, section 12)."""
    client_ = TestClient(app)
    resume_token = _create_full_order(client_)

    response = client_.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    html = response.text

    assert html.count('progress__step is-active') == 1
    li_blocks = html.split("<li ")
    review_block = next(b for b in li_blocks if "Проверка" in b)
    review_state_match = re.search(r'class="progress__step (is-\S+)"', review_block)
    assert review_state_match is not None
    assert review_state_match.group(1) == "is-active"

    for path in ("edit-coverage", "edit-date", "edit-vehicle", "edit-policyholder"):
        assert f'<a href="/o/{resume_token}/{path}" class="progress__num">' in html

    # step 3 ("Способ ввода") must be "done" (completed, neutral) but NOT a link.
    assert '<span class="progress__num">3</span>' in html


def test_post_order_editing_updates_the_same_order_never_creates_a_duplicate():
    """Section 16 test scenario end-to-end: edit coverage (period change,
    price/end_date recompute), date, vehicle, policyholder -- all through
    order-scoped routes, same order id/token throughout, order count stays 1."""
    client_ = TestClient(app)
    before_count = _count_orders()
    resume_token = _create_full_order(client_)
    assert _count_orders() == before_count + 1

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order_before = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    order_id = order_before.id
    assert order_before.price_customer_minor == 1500 * 100  # 15d, from test fixture pricing
    assert order_before.end_date.isoformat() == "2026-08-30"  # 15.08 + 15d

    # --- edit coverage: 15d -> 30d ---
    response = client_.post(
        f"/o/{resume_token}/edit-coverage",
        data={"category_code": "passenger_car", "period_code": "30d"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/o/{resume_token}/summary"

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order_after_coverage = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order_after_coverage.id == order_id
    assert order_after_coverage.resume_token == resume_token
    assert order_after_coverage.period_code == "30d"
    assert order_after_coverage.price_customer_minor == 2500 * 100  # 30d, from test fixture pricing
    assert order_after_coverage.end_date.isoformat() == "2026-09-14"  # same 15.08 start + 30d
    assert order_after_coverage.start_date.isoformat() == "2026-08-15"  # untouched

    response = client_.get(f"/o/{resume_token}/summary")
    assert "2 500" in response.text

    # --- edit date ---
    response = client_.post(f"/o/{resume_token}/edit-date", data={"start_date": "2026-09-01"}, follow_redirects=False)
    assert response.status_code == 303

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order_after_date = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order_after_date.id == order_id
    assert order_after_date.start_date.isoformat() == "2026-09-01"
    assert order_after_date.end_date.isoformat() == "2026-10-01"  # 30d from the new start

    # --- edit vehicle: change model ---
    response = client_.post(
        f"/o/{resume_token}/edit-vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_other_manufacturer_id),
            "model_id": str(_other_model_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client_.get(f"/o/{resume_token}/summary")
    assert "AUDI" in response.text

    # --- edit policyholder ---
    response = client_.post(
        f"/o/{resume_token}/edit-policyholder",
        data={"full_name": "Petrov Petr", "contact_email": "petr@example.com", "contact_telegram": "@petr"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client_.get(f"/o/{resume_token}/summary")
    assert "Petrov Petr" in response.text

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order_final = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order_final.id == order_id
    assert order_final.resume_token == resume_token
    assert _count_orders() == before_count + 1  # still exactly one order created


def test_edit_coverage_rejects_1y_and_does_not_touch_the_order(real_production_config):
    """Uses the real config (1y doesn't exist there at all -- we can't sell
    a 1-year policy) rather than the test fixture, which prices 1y for
    other tests' convenience (e.g. exercising the 1-year date math)."""
    client_ = TestClient(app)
    resume_token = _create_full_order(client_)

    response = client_.post(
        f"/o/{resume_token}/edit-coverage",
        data={"category_code": "passenger_car", "period_code": "1y"},
        follow_redirects=False,
    )
    assert response.status_code == 422

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import get_order_by_token

        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.period_code == "15d"  # unchanged
