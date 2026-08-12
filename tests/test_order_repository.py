from datetime import date

import pytest

from app.catalog.repository import upsert_manufacturer, upsert_model
from app.db import get_connection, init_db
from app.orders.repository import (
    create_order,
    get_order_by_id,
    get_order_by_token,
    set_dates,
    set_period,
    set_status,
    update_vehicle_fields,
)
from app.orders.state_machine import InvalidTransition, OrderStatus
from app.sessions.repository import clear_draft, ensure_session, get_draft, merge_draft, save_draft


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def catalog_ids(conn):
    manufacturer_id = upsert_manufacturer(conn, external_id=12, name="BMW", is_popular=True)
    model_id = upsert_model(conn, external_id=361, manufacturer_id=manufacturer_id, name="730 LD")
    conn.commit()
    return manufacturer_id, model_id


def _create(conn, catalog_ids, session_id="sess-1"):
    manufacturer_id, model_id = catalog_ids
    ensure_session(conn, session_id)
    return create_order(
        conn,
        session_id=session_id,
        country_code="GE",
        vehicle_category_code="passenger_car",
        period_code="15d",
        start_date=date(2026, 8, 15),
        end_date=date(2026, 8, 30),
        price_customer_minor=150000,
        data_entry_method="manual",
        registration_number="A123AA777",
        identifier_type="vin",
        identifier="JT123456789012345",
        manufacturer_id=manufacturer_id,
        manufacturer_name="BMW",
        model_id=model_id,
        model_name="730 LD",
        full_name="Ivanov Ivan",
        contact_type="telegram",
        contact_value="@ivan",
        customer_currency="RUB",
        purchase_currency="GEL",
    )


def test_create_order_sets_public_number_and_resume_token(conn, catalog_ids):
    order = _create(conn, catalog_ids)
    assert order.public_number == f"ORDER-{1000 + order.id}"
    assert len(order.resume_token) >= 32
    assert order.status == OrderStatus.DATA_COMPLETED.value


def test_create_order_stores_catalog_and_identifier_fields(conn, catalog_ids):
    manufacturer_id, model_id = catalog_ids
    order = _create(conn, catalog_ids)
    assert order.vehicle_category_code == "passenger_car"
    assert order.manufacturer_id == manufacturer_id
    assert order.model_id == model_id
    assert order.identifier_type == "vin"
    assert order.identifier == "JT123456789012345"
    assert order.display_registration_number == "A123AA777"
    assert order.data_entry_method == "manual"


def test_create_order_snapshots_manufacturer_and_model_names(conn, catalog_ids):
    """vehicle_make/vehicle_model are a point-in-time snapshot, not a live
    join — see test_order_snapshot_survives_manufacturer_rename below for
    why that distinction matters."""
    order = _create(conn, catalog_ids)
    assert order.vehicle_make == "BMW"
    assert order.vehicle_model == "730 LD"


def test_order_snapshot_survives_manufacturer_rename(conn, catalog_ids):
    manufacturer_id, _ = catalog_ids
    order = _create(conn, catalog_ids)

    # Simulate a later catalog sync renaming the manufacturer.
    upsert_manufacturer(conn, external_id=12, name="BMW AG (renamed)", is_popular=True)
    conn.commit()

    reread = get_order_by_id(conn, order.id)
    assert reread.vehicle_make == "BMW"  # unchanged — snapshot, not a live join
    from app.catalog.repository import get_manufacturer

    assert get_manufacturer(conn, manufacturer_id).name == "BMW AG (renamed)"  # catalog itself did change


def test_order_snapshot_survives_manufacturer_deactivation(conn, catalog_ids):
    manufacturer_id, _ = catalog_ids
    order = _create(conn, catalog_ids)

    from app.catalog.repository import deactivate_manufacturers_not_in, get_manufacturer

    deactivate_manufacturers_not_in(conn, [])  # deactivates everything, including this one
    conn.commit()

    assert get_manufacturer(conn, manufacturer_id) is None  # catalog lookup now filtered out
    reread = get_order_by_id(conn, order.id)
    assert reread.vehicle_make == "BMW"  # summary still shows what the customer actually bought


def test_create_order_writes_full_history_trail(conn, catalog_ids):
    order = _create(conn, catalog_ids)
    rows = conn.execute(
        "SELECT * FROM insurance_order_status_history WHERE order_id = ? ORDER BY id", (order.id,)
    ).fetchall()
    assert [r["to_status"] for r in rows] == [OrderStatus.DRAFT.value, OrderStatus.DATA_COMPLETED.value]
    assert rows[0]["from_status"] is None
    assert rows[1]["from_status"] == OrderStatus.DRAFT.value


def test_get_order_by_token_roundtrip(conn, catalog_ids):
    order = _create(conn, catalog_ids)
    fetched = get_order_by_token(conn, order.resume_token)
    assert fetched is not None
    assert fetched.id == order.id


def test_get_order_by_token_unknown_returns_none(conn):
    assert get_order_by_token(conn, "does-not-exist") is None


def test_set_status_enforces_state_machine(conn, catalog_ids):
    order = _create(conn, catalog_ids)
    set_status(conn, order.id, OrderStatus.AWAITING_PAYMENT)
    updated = get_order_by_id(conn, order.id)
    assert updated.status == OrderStatus.AWAITING_PAYMENT.value

    with pytest.raises(InvalidTransition):
        set_status(conn, order.id, OrderStatus.POLICY_READY)


def test_update_vehicle_fields(conn, catalog_ids):
    order = _create(conn, catalog_ids)
    other_manufacturer_id = upsert_manufacturer(conn, external_id=7, name="AUDI", is_popular=False)
    other_model_id = upsert_model(conn, external_id=99, manufacturer_id=other_manufacturer_id, name="A4")
    conn.commit()

    update_vehicle_fields(
        conn,
        order.id,
        registration_number="B456BB777",
        identifier_type="chassis",
        identifier="CHS12345",
        manufacturer_id=other_manufacturer_id,
        manufacturer_name="AUDI",
        model_id=other_model_id,
        model_name="A4",
    )
    updated = get_order_by_id(conn, order.id)
    assert updated.display_registration_number == "B456BB777"
    assert updated.identifier_type == "chassis"
    assert updated.identifier == "CHS12345"
    assert updated.manufacturer_id == other_manufacturer_id
    assert updated.model_id == other_model_id
    assert updated.vehicle_make == "AUDI"  # re-snapshotted on edit too
    assert updated.vehicle_model == "A4"


def test_legacy_set_period_and_dates_still_work(conn, catalog_ids):
    """set_period/set_dates are only used by the legacy /o/{token}/period and
    /o/{token}/date routes now, but must keep working for any pre-existing
    order that still needs them."""
    order = _create(conn, catalog_ids)
    set_period(conn, order.id, period_code="15d", price_customer_minor=150000)
    set_dates(conn, order.id, start_date=date(2026, 8, 15), end_date=date(2026, 8, 30))
    updated = get_order_by_id(conn, order.id)
    assert updated.period_code == "15d"
    assert updated.price_customer_minor == 150000
    assert updated.start_date == date(2026, 8, 15)
    assert updated.end_date == date(2026, 8, 30)


def test_session_draft_roundtrip(conn):
    ensure_session(conn, "sess-2")
    assert get_draft(conn, "sess-2") is None
    save_draft(conn, "sess-2", {"vehicle_category_code": "passenger_car"})
    assert get_draft(conn, "sess-2") == {"vehicle_category_code": "passenger_car"}


def test_merge_draft_adds_keys_without_clobbering_existing_ones(conn):
    ensure_session(conn, "sess-3")
    merge_draft(conn, "sess-3", {"vehicle_category_code": "passenger_car"})
    merge_draft(conn, "sess-3", {"period_code": "15d"})
    assert get_draft(conn, "sess-3") == {"vehicle_category_code": "passenger_car", "period_code": "15d"}


def test_clear_draft_empties_it(conn):
    ensure_session(conn, "sess-4")
    merge_draft(conn, "sess-4", {"vehicle_category_code": "passenger_car"})
    clear_draft(conn, "sess-4")
    assert get_draft(conn, "sess-4") == {}
