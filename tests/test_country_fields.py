"""Step 4 of the GE/AM/TR rollout: country-specific vehicle/policyholder
fields (engine_power, model_year, date_of_birth).

GE never shows or requires any of the three. AM requires engine_power only.
TR requires model_year and date_of_birth -- NOT engine_power (business
decision, 2026-09-06: the source site, strahovka-turkiye.com, only asks for
engine/motor info for a separate, unrelated "Turkish plates under customs
deposit" service, not for buying the OSAGO policy itself -- see
tests/test_tr_tl_pricing.py's field-behavior coverage for the full flow).
AM still has no real pricing (see tests/test_country_periods.py), so a full
AM order is only reachable by seeding price_customer_minor directly into the
draft -- this file does that purely to test these fields end-to-end through
the real validation/route code, not to claim AM pricing exists.
"""

import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import SCHEMA, _ORDER_COLUMN_MIGRATIONS, get_connection, init_db
from app.deps import SESSION_COOKIE_NAME, get_settings
from app.main import app
from app.orders.models import Order
from app.orders.repository import get_order_by_token
from app.sessions.repository import get_draft, merge_draft
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()

# Own catalog rows -- external_id=17001 continues the per-file numbering
# convention (see tests/test_country_routing.py's own comment on why).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=17001, name="ZFIELDSFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=17001, manufacturer_id=_manufacturer_id, name="ZFIELDSFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=90)


@pytest.fixture
def real_config(monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


def _session_id(client: TestClient) -> str:
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id
    return session_id


def _read_draft(client: TestClient) -> dict:
    conn = get_connection(_settings.app.db_file)
    try:
        return get_draft(conn, _session_id(client)) or {}
    finally:
        conn.close()


def _seed_draft(client: TestClient, **updates) -> None:
    conn = get_connection(_settings.app.db_file)
    try:
        merge_draft(conn, _session_id(client), updates)
    finally:
        conn.close()


def _vehicle_form_data(**overrides) -> dict:
    data = {
        "registration_number": "A123AA777",
        "identifier_type": "vin",
        "identifier": "JT123456789012345",
        "manufacturer_id": str(_manufacturer_id),
        "model_id": str(_model_id),
    }
    data.update(overrides)
    return data


def _order(resume_token: str) -> Order:
    conn = get_connection(_settings.app.db_file)
    try:
        return get_order_by_token(conn, resume_token)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Georgia: no new fields shown, required, or stored
# ---------------------------------------------------------------------------


def test_ge_vehicle_form_does_not_show_engine_power_or_model_year():
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": _START.isoformat()})
    client.post("/method", data={"choice": "manual"})
    response = client.get("/vehicle")
    assert response.status_code == 200
    assert 'name="engine_power"' not in response.text
    assert 'name="model_year"' not in response.text


def test_ge_policyholder_does_not_show_date_of_birth():
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": _START.isoformat()})
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_form_data())
    response = client.get("/policyholder")
    assert response.status_code == 200
    assert 'name="date_of_birth"' not in response.text


def test_ge_happy_path_unchanged_and_order_gets_none_in_all_three_new_fields():
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": _START.isoformat()})
    client.post("/method", data={"choice": "manual"})
    client.post("/vehicle", data=_vehicle_form_data())
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@ge_fields"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    order = _order(resume_token)
    assert order.engine_power is None
    assert order.model_year is None
    assert order.date_of_birth is None

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "Мощность двигателя" not in summary.text
    assert "Год выпуска" not in summary.text
    assert "Дата рождения" not in summary.text
    assert "None" not in summary.text


# ---------------------------------------------------------------------------
# Armenia: engine_power only
# ---------------------------------------------------------------------------


def _reach_am_vehicle_screen(client: TestClient) -> None:
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    client.post("/date", data={"start_date": _START.isoformat(), "end_date": (_START + timedelta(days=10)).isoformat()})
    client.post("/method", data={"choice": "manual"})


def test_am_vehicle_form_shows_engine_power_not_model_year(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    response = client.get("/vehicle")
    assert response.status_code == 200
    assert 'name="engine_power"' in response.text
    assert 'name="model_year"' not in response.text


def test_am_policyholder_does_not_show_date_of_birth(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(engine_power="150"))
    response = client.get("/policyholder")
    assert response.status_code == 200
    assert 'name="date_of_birth"' not in response.text


def test_am_engine_power_required_server_side(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data())  # no engine_power
    assert response.status_code == 422
    assert "Мощность двигателя: заполните это поле" in response.text


def test_am_valid_engine_power_survives_draft(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data(engine_power="150"), follow_redirects=False)
    assert response.status_code == 303
    draft = _read_draft(client)
    assert draft["engine_power"] == 150
    assert "model_year" not in draft or draft["model_year"] is None


def test_am_engine_power_saved_correctly_when_order_creation_is_exercised(real_config):
    """AM has no real price yet (see test_country_periods.py) -- the price
    is seeded directly into the draft here purely to reach create_order and
    verify engine_power actually lands on the Order, not to claim AM
    pricing exists."""
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(engine_power="150"))
    _seed_draft(client, price_customer_minor=250000)

    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_fields"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    order = _order(resume_token)
    assert order.engine_power == 150
    assert order.model_year is None
    assert order.date_of_birth is None


def test_am_summary_shows_engine_power_only(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(engine_power="150"))
    _seed_draft(client, price_customer_minor=250000)
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_summary"), follow_redirects=False
    )
    resume_token = response.headers["location"].split("/")[2]

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "Мощность двигателя" in summary.text
    assert "150 л.с." in summary.text
    assert "Год выпуска" not in summary.text
    assert "Дата рождения" not in summary.text
    assert "None" not in summary.text


# ---------------------------------------------------------------------------
# Turkey: engine_power + model_year + date_of_birth
# ---------------------------------------------------------------------------


def _reach_tr_vehicle_screen(client: TestClient) -> None:
    """TR now has real TL-based pricing (see tests/test_tr_tl_pricing.py),
    but this file still seeds the draft directly with a fixed period +
    price to reach /vehicle -- a minimal shortcut to isolate a field/route
    test, not a claim that TR pricing doesn't exist. Both /vehicle's own
    guard and data_entry_method are satisfied directly by the seed, so no
    /method POST is needed first."""
    _start(client, "TR")
    _seed_draft(
        client,
        vehicle_category_code="passenger_car",
        period_code="30d",
        price_customer_minor=500000,
        start_date=_START.isoformat(),
        end_date=(_START + timedelta(days=30)).isoformat(),
        data_entry_method="manual",
    )


def test_tr_vehicle_form_shows_model_year_not_engine_power(real_config):
    """engine_power is AM-only now -- TR no longer shows it at all (business
    decision, 2026-09-06)."""
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    response = client.get("/vehicle")
    assert response.status_code == 200
    assert 'name="engine_power"' not in response.text
    assert 'name="model_year"' in response.text


def test_tr_policyholder_shows_date_of_birth(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(model_year="2020"))
    response = client.get("/policyholder")
    assert response.status_code == 200
    assert 'name="date_of_birth"' in response.text


def test_tr_model_year_required_engine_power_not_required_server_side(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data())  # neither field
    assert response.status_code == 422
    assert "Мощность двигателя" not in response.text
    assert "Год выпуска: заполните это поле" in response.text


def test_tr_vehicle_submission_succeeds_without_engine_power(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data(model_year="2020"), follow_redirects=False)
    assert response.status_code == 303


def test_tr_date_of_birth_required_server_side(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(model_year="2020"))
    response = client.post("/policyholder", data=valid_policyholder_data(contact_telegram="@tr_no_dob"))
    assert response.status_code == 422
    assert "Дата рождения: заполните это поле" in response.text


def test_tr_model_year_and_dob_survive_engine_power_is_ignored(real_config):
    """A stray engine_power value in the raw POST (e.g. a stale/tampered
    submission from before this field was hidden) must be silently
    discarded, never validated or stored -- same "GE never asks, so it
    always stays None regardless of a stale submission" rule post_vehicle
    already documents for GE, now also true for TR."""
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    client.post(
        "/vehicle", data=_vehicle_form_data(engine_power="150", model_year="2020"), follow_redirects=False
    )
    draft = _read_draft(client)
    assert draft["model_year"] == 2020
    assert draft.get("engine_power") is None

    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_fields", date_of_birth="1990-05-20"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    order = _order(resume_token)
    assert order.engine_power is None
    assert order.model_year == 2020
    assert order.date_of_birth.isoformat() == "1990-05-20"


def test_tr_summary_shows_model_year_and_dob_not_engine_power(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(model_year="2020"))
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_summary", date_of_birth="1990-05-20"),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "Мощность двигателя" not in summary.text
    assert "Год выпуска" in summary.text
    assert "2020" in summary.text
    assert "Дата рождения" in summary.text
    assert "20.05.1990" in summary.text
    assert "None" not in summary.text


def test_tr_edit_vehicle_also_omits_engine_power(real_config):
    """Same category-aware... well, country-aware here (TR has no
    engine-less-category concept, it's ALL of TR) rule applies post-order,
    on the edit-vehicle screen (a second render path through the same
    _vehicle_form_context)."""
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(model_year="2020"))
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_edit_no_engine", date_of_birth="1990-05-20"),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]

    edit_get = client.get(f"/o/{resume_token}/edit-vehicle")
    assert edit_get.status_code == 200
    assert 'name="engine_power"' not in edit_get.text


# ---------------------------------------------------------------------------
# Validation (direct, cheap, exercised via whichever country requires the field)
# ---------------------------------------------------------------------------


def test_engine_power_invalid_text_rejected(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data(engine_power="abc"))
    assert response.status_code == 422
    assert "Мощность двигателя: укажите число в лошадиных силах" in response.text


def test_engine_power_zero_rejected(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data(engine_power="0"))
    assert response.status_code == 422
    assert "Мощность двигателя: укажите число больше нуля" in response.text


def test_engine_power_valid_accepted(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data(engine_power="300"), follow_redirects=False)
    assert response.status_code == 303


def test_engine_power_above_upper_bound_rejected(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data(engine_power="99999"))
    assert response.status_code == 422
    assert "Мощность двигателя: слишком большое значение" in response.text


def test_invalid_model_year_text_rejected(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    response = client.post("/vehicle", data=_vehicle_form_data(engine_power="150", model_year="not-a-year"))
    assert response.status_code == 422
    assert "Год выпуска: укажите год числом" in response.text


def test_model_year_too_far_in_the_future_rejected(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    too_far = today_in_georgia().year + 2
    response = client.post("/vehicle", data=_vehicle_form_data(engine_power="150", model_year=str(too_far)))
    assert response.status_code == 422
    assert "Год выпуска: слишком позднее значение" in response.text


def test_model_year_next_year_is_accepted(real_config):
    """Matches the automotive industry's own practice of selling next
    year's model in advance."""
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    next_year = today_in_georgia().year + 1
    response = client.post(
        "/vehicle", data=_vehicle_form_data(engine_power="150", model_year=str(next_year)), follow_redirects=False
    )
    assert response.status_code == 303


def test_future_date_of_birth_rejected(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(engine_power="150", model_year="2020"))
    future_dob = (today_in_georgia() + timedelta(days=1)).isoformat()
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@future_dob", date_of_birth=future_dob)
    )
    assert response.status_code == 422
    assert "Дата рождения: не может быть в будущем" in response.text


def test_valid_date_of_birth_accepted(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(engine_power="150", model_year="2020"))
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@valid_dob", date_of_birth="1985-01-15"),
        follow_redirects=False,
    )
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# Edit flow (section 11: same edit-vehicle/edit-policyholder routes, no new
# edit pages)
# ---------------------------------------------------------------------------


def test_edit_vehicle_updates_engine_power_for_an_am_order(real_config):
    client = TestClient(app)
    _reach_am_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(engine_power="150"))
    _seed_draft(client, price_customer_minor=250000)
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_edit"), follow_redirects=False
    )
    resume_token = response.headers["location"].split("/")[2]
    assert _order(resume_token).engine_power == 150

    edit_get = client.get(f"/o/{resume_token}/edit-vehicle")
    assert edit_get.status_code == 200
    assert 'value="150"' in edit_get.text
    assert 'name="model_year"' not in edit_get.text  # AM order -- still no model_year field

    edit_post = client.post(
        f"/o/{resume_token}/edit-vehicle", data=_vehicle_form_data(engine_power="220"), follow_redirects=False
    )
    assert edit_post.status_code == 303
    assert _order(resume_token).engine_power == 220


def test_edit_policyholder_updates_date_of_birth_for_a_tr_order(real_config):
    client = TestClient(app)
    _reach_tr_vehicle_screen(client)
    client.post("/vehicle", data=_vehicle_form_data(engine_power="150", model_year="2020"))
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_edit", date_of_birth="1990-05-20"),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]
    assert _order(resume_token).date_of_birth.isoformat() == "1990-05-20"

    edit_get = client.get(f"/o/{resume_token}/edit-policyholder")
    assert edit_get.status_code == 200
    assert 'value="1990-05-20"' in edit_get.text

    edit_post = client.post(
        f"/o/{resume_token}/edit-policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_edit", date_of_birth="1985-03-10"),
        follow_redirects=False,
    )
    assert edit_post.status_code == 303
    assert _order(resume_token).date_of_birth.isoformat() == "1985-03-10"


# ---------------------------------------------------------------------------
# Migration safety
# ---------------------------------------------------------------------------


def test_migration_adds_new_columns_without_touching_existing_rows(tmp_path):
    """Simulates upgrading a pre-Step-4 database: the full schema plus
    every migration EXCEPT this step's three new columns (init_db's own
    migrations are additive/idempotent -- see app.db._migrate_columns --
    so this is a faithful stand-in for "an older version of the schema"
    without needing a second, frozen copy of it). A real GE-shaped row is
    inserted using only pre-Step-4 columns, then the CURRENT init_db runs
    against the same file: the new columns must appear (NULL), and the
    existing row's own data must be completely untouched."""
    db_path = tmp_path / "pre_step4.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for column, definition in _ORDER_COLUMN_MIGRATIONS:
        if column in ("engine_power", "model_year", "date_of_birth"):
            continue
        conn.execute(f"ALTER TABLE insurance_orders ADD COLUMN {column} {definition}")
    conn.commit()

    conn.execute(
        """
        INSERT INTO insurance_orders (
            public_number, country_code, status, session_id, full_name,
            period_code, start_date, end_date, customer_currency, purchase_currency,
            resume_token, created_at, updated_at,
            driver_same_as_policyholder, owner_same_as_policyholder
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "PRE-STEP4-1",
            "GE",
            "paid",
            "sess-pre-step4",
            "Ivanov Ivan",
            "15d",
            "2026-08-15",
            "2026-08-30",
            "RUB",
            "GEL",
            "tok-pre-step4",
            "2026-08-01T00:00:00",
            "2026-08-01T00:00:00",
            1,
            1,
        ),
    )
    conn.commit()
    conn.close()

    init_db(db_path)  # the CURRENT (post-Step-4) migration set

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM insurance_orders WHERE public_number = ?", ("PRE-STEP4-1",)
        ).fetchone()
        assert row is not None
        assert row["full_name"] == "Ivanov Ivan"  # untouched
        assert row["engine_power"] is None
        assert row["model_year"] is None
        assert row["date_of_birth"] is None

        order = Order.from_row(row)
        assert order.full_name == "Ivanov Ivan"
        assert order.engine_power is None
        assert order.model_year is None
        assert order.date_of_birth is None
    finally:
        conn.close()
