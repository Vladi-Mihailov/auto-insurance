"""GE -> TPL policy issuance: pure logic (live product resolution, citizenship
mapping, payload building) plus the issue_tpl_policy orchestration, with every
outbound HTTP call monkeypatched at the module-function boundary -- same
convention as tests/test_catalog_sync.py. No real TPL/BOG request anywhere in
this file."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.catalog.repository import upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection, init_db
from app.integrations.tpl_ge import repository as tpl_repo
from app.integrations.tpl_ge import service
from app.integrations.tpl_ge.errors import (
    BogHandoffError,
    MissingRequiredDataError,
    ProductNotFoundError,
    TplApplicationError,
    TplIssuanceError,
    VisitorIdNotConfiguredError,
)
from app.orders.models import Order
from app.orders.repository import create_order, get_order_by_id, set_status
from app.orders.state_machine import OrderStatus
from app.settings import TplGeSettings

# Own catalog rows -- external_id=26001 continues the per-file numbering
# convention (see tests/test_operator_notifications.py for the last one).
_MANUFACTURER_EXT_ID = 26001
_MODEL_EXT_ID = 26001
_CATEGORY_EXT_ID = 7  # passenger_car, already mapped by app.catalog.sync

_TODAY = date(2026, 9, 7)

_LIVE_CATEGORIES = [
    {
        "id": _CATEGORY_EXT_ID,
        "products": [
            {
                "productId": 1,
                "period": 15,
                "periodType": "D",
                "price": 30.0,
                "minDate": "2026-09-06T19:06:03",
                "maxDate": "2026-12-05T19:06:03",
            },
            {
                "productId": 2,
                "period": 30,
                "periodType": "D",
                "price": 50.0,
                "minDate": "2026-09-06T19:06:03",
                "maxDate": "2026-12-05T19:06:03",
            },
        ],
    }
]

_LIVE_COUNTRIES = [
    {"id": 52, "name": "Russia"},
    {"id": 1, "name": "Georgia"},
]


# ---------------------------------------------------------------------------
# resolve_product
# ---------------------------------------------------------------------------


def test_resolve_product_matches_category_and_period():
    product = service.resolve_product(_LIVE_CATEGORIES, _CATEGORY_EXT_ID, "15d", order_start_date=_TODAY)
    assert product.product_id == 1
    assert product.price_gel == Decimal("30.0")


def test_resolve_product_rejects_unknown_category():
    with pytest.raises(ProductNotFoundError):
        service.resolve_product(_LIVE_CATEGORIES, 999, "15d", order_start_date=_TODAY)


def test_resolve_product_rejects_unknown_period():
    with pytest.raises(ProductNotFoundError):
        service.resolve_product(_LIVE_CATEGORIES, _CATEGORY_EXT_ID, "45d", order_start_date=_TODAY)


def test_resolve_product_rejects_malformed_period_code():
    with pytest.raises(ProductNotFoundError):
        service.resolve_product(_LIVE_CATEGORIES, _CATEGORY_EXT_ID, "annual", order_start_date=_TODAY)


def test_resolve_product_rejects_start_date_outside_live_window():
    with pytest.raises(ProductNotFoundError):
        service.resolve_product(
            _LIVE_CATEGORIES, _CATEGORY_EXT_ID, "15d", order_start_date=date(2027, 1, 1)
        )


# ---------------------------------------------------------------------------
# resolve_citizenship_id
# ---------------------------------------------------------------------------


def test_resolve_citizenship_id_exact_match():
    assert service.resolve_citizenship_id(_LIVE_COUNTRIES, "Russia") == 52


def test_resolve_citizenship_id_via_known_alias():
    assert service.resolve_citizenship_id(_LIVE_COUNTRIES, "Russian Federation") == 52


def test_resolve_citizenship_id_rejects_unknown_country():
    with pytest.raises(MissingRequiredDataError):
        service.resolve_citizenship_id(_LIVE_COUNTRIES, "Wakanda")


def test_resolve_citizenship_id_rejects_country_missing_from_tpl_list():
    with pytest.raises(MissingRequiredDataError):
        service.resolve_citizenship_id([{"id": 1, "name": "Georgia"}], "Russia")


# ---------------------------------------------------------------------------
# build_application_payload
# ---------------------------------------------------------------------------

_PRODUCT = service.LiveProduct(
    product_id=1, period=15, period_type="D", price_gel=Decimal("30.0"), min_date=_TODAY, max_date=_TODAY
)


def _order(**overrides) -> Order:
    defaults = dict(
        id=1,
        public_number="ORDER-0001",
        country_code="GE",
        status=OrderStatus.PAID.value,
        session_id="sess",
        full_name="Ivanov Ivan",
        identification_number="AB1234567",
        citizenship="Russia",
        contact_email="ivan@example.com",
        contact_telegram=None,
        contact_phone="+79991234567",
        contact_max=None,
        contact_other=None,
        period_code="15d",
        start_date=_TODAY,
        end_date=_TODAY + timedelta(days=15),
        customer_currency="RUB",
        purchase_currency="GEL",
        price_customer_minor=134900,
        resume_token="tok",
        created_at=None,
        updated_at=None,
        vehicle_category_code="passenger_car",
        manufacturer_id=1,
        model_id=1,
        identifier_type="vin",
        identifier="JYARJ41E7KA000900",
        data_entry_method="manual",
        engine_power=None,
        model_year=None,
        date_of_birth=None,
        vehicle_make="BMW",
        vehicle_model="Other",
        vin=None,
        car_number="AB123CD",
        contact_type=None,
        contact_value=None,
        driver_same_as_policyholder=True,
        driver_full_name=None,
        driver_identifier=None,
        driver_citizenship=None,
        driver_phone=None,
        driver_email=None,
        owner_same_as_policyholder=True,
        owner_entity_type=None,
        owner_full_name=None,
        owner_identifier=None,
        owner_citizenship=None,
        owner_phone=None,
        owner_email=None,
    )
    defaults.update(overrides)
    return Order(**defaults)


def _payload(**order_overrides) -> dict:
    order = _order(**order_overrides)
    return service.build_application_payload(
        order,
        uid="00000000-0000-0000-0000-000000000000",
        product=_PRODUCT,
        category_external_id=7,
        manufacturer_external_id=_MANUFACTURER_EXT_ID,
        model_external_id=_MODEL_EXT_ID,
        insurer_citizenship_id=52,
        owner_citizenship_id=52,
        driver_citizenship_id=52,
        visitor_id="captured-visitor-id",
    )


def test_build_payload_insurer_equals_owner_equals_driver_mvp():
    """Confirmed checkout scenario: same_as_policyholder=True for both
    owner and driver -- the DB stores NULL for their own fields (see
    app.orders.models.Order), so this must fall back to the policyholder's
    own identity for both roles."""
    payload = _payload()
    assert payload["insurerTitle"] == "Ivanov Ivan"
    assert payload["vehicleOwnerTitle"] == "Ivanov Ivan"
    assert payload["vehicleDriverTitle"] == "Ivanov Ivan"
    assert payload["vehicleOwnerIdentificationNumber"] == "AB1234567"
    assert payload["vehicleDriverIdentificationNumber"] == "AB1234567"
    assert payload["borderCrossId"] is None
    assert payload["lang"] == "ru"
    assert payload["productId"] == 1
    assert payload["vinCode"] == "JYARJ41E7KA000900"
    assert payload["visitorId"] == "captured-visitor-id"


def test_build_payload_uses_distinct_owner_and_driver_when_not_same_as():
    payload = _payload(
        owner_same_as_policyholder=False,
        owner_entity_type="individual",
        owner_full_name="Petrov Petr",
        owner_identifier="CD7654321",
        owner_citizenship="Georgia",
        owner_phone="+79990000000",
        owner_email="petrov@example.com",
        driver_same_as_policyholder=False,
        driver_full_name="Sidorov Petr",
        driver_identifier="EF1112223",
        driver_citizenship="Georgia",
        driver_phone="+79998887766",
        driver_email="sidorov@example.com",
    )
    assert payload["vehicleOwnerTitle"] == "Petrov Petr"
    assert payload["vehicleDriverTitle"] == "Sidorov Petr"


def test_build_payload_rejects_legal_entity_owner_as_mvp_limitation():
    with pytest.raises(MissingRequiredDataError):
        _payload(owner_same_as_policyholder=False, owner_entity_type="legal", owner_full_name="ACME LLC", owner_identifier="123")


@pytest.mark.parametrize(
    "field, value",
    [
        ("contact_phone", None),
        ("contact_email", None),
        ("full_name", None),
        ("identification_number", None),
        ("car_number", None),
        ("identifier", None),
    ],
)
def test_build_payload_rejects_missing_required_data_rather_than_faking_it(field, value):
    with pytest.raises(MissingRequiredDataError):
        _payload(**{field: value})


# ---------------------------------------------------------------------------
# issue_tpl_policy orchestration -- monkeypatched HTTP boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def catalog_ids(conn):
    """(manufacturer_id, model_id) -- internal catalog row ids, deliberately
    distinct from their external_id (see app.catalog.models), matching the
    real translation issue.integrations.tpl_ge.service._resolve_catalog_external_ids
    exists to solve."""
    upsert_category(conn, external_id=_CATEGORY_EXT_ID, code="passenger_car", name="Легковой", icon=None)
    manufacturer_id = upsert_manufacturer(conn, external_id=_MANUFACTURER_EXT_ID, name="BMW", is_popular=False)
    model_id = upsert_model(conn, external_id=_MODEL_EXT_ID, manufacturer_id=manufacturer_id, name="Other")
    conn.commit()
    return manufacturer_id, model_id


def _paid_order(conn, catalog_ids, **overrides):
    manufacturer_id, model_id = catalog_ids
    kwargs = dict(
        session_id="sess",
        country_code="GE",
        vehicle_category_code="passenger_car",
        period_code="15d",
        start_date=_TODAY,
        end_date=_TODAY + timedelta(days=15),
        price_customer_minor=134900,
        data_entry_method="manual",
        registration_number="AB123CD",
        identifier_type="vin",
        identifier="JYARJ41E7KA000900",
        manufacturer_id=manufacturer_id,
        manufacturer_name="BMW",
        model_id=model_id,
        model_name="Other",
        full_name="Ivanov Ivan",
        contact_email="ivan@example.com",
        contact_telegram=None,
        contact_phone="+79991234567",
        contact_max=None,
        contact_other=None,
        customer_currency="RUB",
        purchase_currency="GEL",
        identification_number="AB1234567",
        citizenship="Russia",
    )
    kwargs.update(overrides)
    order = create_order(conn, **kwargs)
    set_status(conn, order.id, OrderStatus.AWAITING_PAYMENT)
    set_status(conn, order.id, OrderStatus.PAYMENT_REVIEW)
    set_status(conn, order.id, OrderStatus.PAID)
    return get_order_by_id(conn, order.id)


@pytest.fixture
def configured_settings():
    return TplGeSettings(static_visitor_id="captured-visitor-id")


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_http_layer(monkeypatch, *, create_application=None, initiate_bog_payment=None):
    monkeypatch.setattr(service.tpl_client, "new_client", lambda: _FakeClient())
    monkeypatch.setattr(service.catalog_client, "fetch_categories", lambda client: _LIVE_CATEGORIES)
    monkeypatch.setattr(service.catalog_client, "fetch_countries", lambda client: _LIVE_COUNTRIES)
    calls = {"create_application": 0, "initiate_bog_payment": []}

    def _default_create(client, payload):
        calls["create_application"] += 1
        if create_application:
            create_application(payload)

    def _default_initiate(client, params):
        calls["initiate_bog_payment"].append(params)
        if initiate_bog_payment:
            return initiate_bog_payment(params)
        return "https://mpi.gc.ge/page1?merch_id=abc&o.id=xyz"

    monkeypatch.setattr(service.tpl_client, "create_application", _default_create)
    monkeypatch.setattr(service.tpl_client, "initiate_bog_payment", _default_initiate)
    return calls


def test_issue_tpl_policy_creates_application_and_returns_bog_link(conn, catalog_ids, monkeypatch, configured_settings):
    calls = _patch_http_layer(monkeypatch)
    order = _paid_order(conn, catalog_ids)

    issuance = service.issue_tpl_policy(conn, order, _settings_with(configured_settings))

    assert calls["create_application"] == 1
    assert len(calls["initiate_bog_payment"]) == 1
    assert issuance.is_bog_link_ready
    assert issuance.bog_payment_url == "https://mpi.gc.ge/page1?merch_id=abc&o.id=xyz"
    assert issuance.tpl_product_id == 1
    assert issuance.tpl_purchase_price_gel == Decimal("30.0")


def test_issue_tpl_policy_moves_order_to_processing(conn, catalog_ids, monkeypatch, configured_settings):
    _patch_http_layer(monkeypatch)
    order = _paid_order(conn, catalog_ids)
    service.issue_tpl_policy(conn, order, _settings_with(configured_settings))

    refreshed = get_order_by_id(conn, order.id)
    assert refreshed.status == OrderStatus.PROCESSING.value


def test_issue_tpl_policy_never_sends_a_second_application_on_repeat_call(conn, catalog_ids, monkeypatch, configured_settings):
    calls = _patch_http_layer(monkeypatch)
    order = _paid_order(conn, catalog_ids)
    settings = _settings_with(configured_settings)

    service.issue_tpl_policy(conn, order, settings)
    refreshed = get_order_by_id(conn, order.id)  # now PROCESSING
    service.issue_tpl_policy(conn, refreshed, settings)  # "Получить новую ссылку"

    assert calls["create_application"] == 1  # never sent twice
    assert len(calls["initiate_bog_payment"]) == 2  # refreshed both times


def test_issue_tpl_policy_reuses_the_same_uid_across_calls(conn, catalog_ids, monkeypatch, configured_settings):
    calls = _patch_http_layer(monkeypatch)
    order = _paid_order(conn, catalog_ids)
    settings = _settings_with(configured_settings)

    service.issue_tpl_policy(conn, order, settings)
    issuance_after_first = tpl_repo.get_issuance_by_order_id(conn, order.id)

    refreshed = get_order_by_id(conn, order.id)
    service.issue_tpl_policy(conn, refreshed, settings)
    issuance_after_second = tpl_repo.get_issuance_by_order_id(conn, order.id)

    assert issuance_after_first.tpl_uid == issuance_after_second.tpl_uid
    first_params, second_params = calls["initiate_bog_payment"]
    assert first_params["policyUId"] == second_params["policyUId"] == issuance_after_first.tpl_uid


def test_issue_tpl_policy_refuses_without_configured_visitor_id(conn, catalog_ids, monkeypatch):
    _patch_http_layer(monkeypatch)
    order = _paid_order(conn, catalog_ids)
    with pytest.raises(VisitorIdNotConfiguredError):
        service.issue_tpl_policy(conn, order, _settings_with(TplGeSettings(static_visitor_id=None)))


def test_issue_tpl_policy_rejects_non_ge_orders(conn, catalog_ids, monkeypatch, configured_settings):
    _patch_http_layer(monkeypatch)
    order = _paid_order(conn, catalog_ids, country_code="AM")
    with pytest.raises(TplIssuanceError):
        service.issue_tpl_policy(conn, order, _settings_with(configured_settings))


def test_issue_tpl_policy_surfaces_application_failure_and_does_not_advance_order(conn, catalog_ids, monkeypatch, configured_settings):
    def _boom(payload):
        raise service.tpl_client.TplPoliciesError("simulated rejection")

    _patch_http_layer(monkeypatch, create_application=_boom)
    order = _paid_order(conn, catalog_ids)

    with pytest.raises(TplApplicationError):
        service.issue_tpl_policy(conn, order, _settings_with(configured_settings))

    refreshed = get_order_by_id(conn, order.id)
    assert refreshed.status == OrderStatus.PAID.value  # never advanced to PROCESSING
    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    assert issuance.is_failed
    assert issuance.last_error


def test_issue_tpl_policy_bog_handoff_failure_does_not_erase_application_created(conn, catalog_ids, monkeypatch, configured_settings):
    def _boom(params):
        raise service.tpl_client.BogHandoffHttpError("simulated BOG outage")

    calls = _patch_http_layer(monkeypatch, initiate_bog_payment=_boom)
    order = _paid_order(conn, catalog_ids)

    with pytest.raises(BogHandoffError):
        service.issue_tpl_policy(conn, order, _settings_with(configured_settings))

    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    assert issuance.application_already_created  # not erased by the later failure
    assert issuance.last_error

    # A later retry must not re-send the application, only the BOG handoff.
    monkeypatch.setattr(service.tpl_client, "initiate_bog_payment", lambda client, params: "https://mpi.gc.ge/page1?ok=1")
    refreshed = get_order_by_id(conn, order.id)
    service.issue_tpl_policy(conn, refreshed, _settings_with(configured_settings))
    assert calls["create_application"] == 1


def _settings_with(tpl_ge: TplGeSettings):
    from app.deps import get_settings

    base = get_settings()
    return base.model_copy(update={"tpl_ge": tpl_ge})
