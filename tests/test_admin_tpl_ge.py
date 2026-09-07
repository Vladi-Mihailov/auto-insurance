"""/admin/orders GE TPL issuance action: button gating, the create-or-refresh
click, idempotency through the real HTTP layer, and AM/TR regression.

Same conventions as tests/test_operator_notifications.py: TestClient +
Basic Auth for admin, own catalog rows (external_id=27001 -- next free block
after test_tpl_ge_service.py's 26001), every outbound TPL/BOG call
monkeypatched at the module-function boundary (see
app.integrations.tpl_ge.service), never a real request.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import get_settings
from app.integrations.tpl_ge import repository as tpl_repo
from app.integrations.tpl_ge import service as tpl_ge_service
from app.main import app
from app.orders.repository import create_order, get_order_by_token, set_status
from app.orders.state_machine import OrderStatus
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()

_MANUFACTURER_EXT_ID = 27001
_MODEL_EXT_ID = 27001

_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=_MANUFACTURER_EXT_ID, name="ZADMINTPLFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=_MODEL_EXT_ID, manufacturer_id=_manufacturer_id, name="ZADMINTPLFICTIONALMODEL")
_conn.commit()
_conn.close()

_START = today_in_georgia() + timedelta(days=90)


@pytest.fixture
def admin_configured(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-test-only")
    monkeypatch.setenv("TPL_GE_STATIC_VISITOR_ID", "captured-visitor-id")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _admin_get(client: TestClient):
    return client.get("/admin/orders", auth=("admin", "s3cret-test-only"))


def _admin_post(client: TestClient, path: str):
    return client.post(path, auth=("admin", "s3cret-test-only"), follow_redirects=False)


def _order(resume_token):
    conn = get_connection(_settings.app.db_file)
    try:
        return get_order_by_token(conn, resume_token)
    finally:
        conn.close()


def _create_paid_order(*, plate: str, country_code: str = "GE") -> str:
    """Builds a PAID order directly via the repository (not the full HTTP
    checkout) -- this file is about the admin TPL action, not checkout
    itself, same shortcut tests/test_tpl_ge_service.py already takes."""
    conn = get_connection(_settings.app.db_file)
    try:
        order = create_order(
            conn,
            session_id=f"sess-{plate}",
            country_code=country_code,
            vehicle_category_code="passenger_car",
            period_code="15d",
            start_date=_START,
            end_date=_START + timedelta(days=15),
            price_customer_minor=134900,
            data_entry_method="manual",
            registration_number=plate,
            identifier_type="vin",
            identifier="JYARJ41E7KA000900",
            manufacturer_id=_manufacturer_id,
            manufacturer_name="ZADMINTPLFICTIONALMAKE",
            model_id=_model_id,
            model_name="ZADMINTPLFICTIONALMODEL",
            **valid_policyholder_data(),
            contact_telegram=None,
            contact_phone="+79991234567",
            contact_max=None,
            contact_other=None,
            customer_currency="RUB",
            purchase_currency="GEL",
        )
        set_status(conn, order.id, OrderStatus.AWAITING_PAYMENT)
        set_status(conn, order.id, OrderStatus.PAYMENT_REVIEW)
        set_status(conn, order.id, OrderStatus.PAID)
        return order.resume_token
    finally:
        conn.close()


def _patch_http_layer(monkeypatch, *, bog_url="https://mpi.gc.ge/page1?merch_id=abc&o.id=xyz"):
    live_categories = [
        {
            "id": 7,
            "products": [
                {
                    "productId": 1,
                    "period": 15,
                    "periodType": "D",
                    "price": 30.0,
                    "minDate": "2020-01-01T00:00:00",
                    "maxDate": "2030-01-01T00:00:00",
                }
            ],
        }
    ]
    live_countries = [{"id": 52, "name": "Russia"}, {"id": 1, "name": "Georgia"}]

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls = {"create_application": 0, "initiate_bog_payment": 0}
    monkeypatch.setattr(tpl_ge_service.tpl_client, "new_client", lambda: _FakeClient())
    monkeypatch.setattr(tpl_ge_service.catalog_client, "fetch_categories", lambda client: live_categories)
    monkeypatch.setattr(tpl_ge_service.catalog_client, "fetch_countries", lambda client: live_countries)

    def _create_application(client, payload):
        calls["create_application"] += 1

    def _initiate_bog_payment(client, params):
        calls["initiate_bog_payment"] += 1
        return bog_url

    monkeypatch.setattr(tpl_ge_service.tpl_client, "create_application", _create_application)
    monkeypatch.setattr(tpl_ge_service.tpl_client, "initiate_bog_payment", _initiate_bog_payment)
    return calls


# ---------------------------------------------------------------------------
# Button gating
# ---------------------------------------------------------------------------


def test_tpl_issue_button_shown_for_ge_paid_order(admin_configured):
    resume_token = _create_paid_order(plate="GEPAID1")
    response = _admin_get(TestClient(app))
    idx = response.text.find(_order(resume_token).public_number)
    card = response.text[idx : idx + 1500]
    assert "Оформить полис TPL" in card
    assert f"/admin/orders/{resume_token}/tpl/issue" in card


def test_tpl_issue_button_not_shown_for_am_order(admin_configured):
    """GE-only -- AM (and TR) must never show any TPL issuance UI at all,
    regardless of status."""
    resume_token = _create_paid_order(plate="AMPAID1", country_code="AM")
    response = _admin_get(TestClient(app))
    idx = response.text.find(_order(resume_token).public_number)
    card = response.text[idx : idx + 1500]
    assert "Оформить полис TPL" not in card
    assert "tpl/issue" not in card


def test_tpl_issue_button_not_shown_for_data_completed_status(admin_configured):
    """Only PAID/PROCESSING show the TPL card -- an earlier GE status must
    not."""
    conn = get_connection(_settings.app.db_file)
    try:
        order = create_order(
            conn,
            session_id="sess-GEDC1",
            country_code="GE",
            vehicle_category_code="passenger_car",
            period_code="15d",
            start_date=_START,
            end_date=_START + timedelta(days=15),
            price_customer_minor=134900,
            data_entry_method="manual",
            registration_number="GEDC1",
            identifier_type="vin",
            identifier="JYARJ41E7KA000900",
            manufacturer_id=_manufacturer_id,
            manufacturer_name="ZADMINTPLFICTIONALMAKE",
            model_id=_model_id,
            model_name="ZADMINTPLFICTIONALMODEL",
            **valid_policyholder_data(),
            contact_telegram=None,
            contact_phone="+79991234567",
            contact_max=None,
            contact_other=None,
            customer_currency="RUB",
            purchase_currency="GEL",
        )
        resume_token = order.resume_token
    finally:
        conn.close()

    response = _admin_get(TestClient(app))
    idx = response.text.find(_order(resume_token).public_number)
    card = response.text[idx : idx + 1500]
    assert "Оформить полис TPL" not in card


# ---------------------------------------------------------------------------
# Click-through flow
# ---------------------------------------------------------------------------


def test_clicking_issue_creates_application_moves_order_and_shows_pay_button(admin_configured, monkeypatch):
    calls = _patch_http_layer(monkeypatch)
    resume_token = _create_paid_order(plate="GEFLOW1")

    response = _admin_post(TestClient(app), f"/admin/orders/{resume_token}/tpl/issue")
    assert response.status_code == 303

    assert _order(resume_token).status == OrderStatus.PROCESSING.value
    assert calls["create_application"] == 1
    assert calls["initiate_bog_payment"] == 1

    page = _admin_get(TestClient(app))
    idx = page.text.find(_order(resume_token).public_number)
    card = page.text[idx : idx + 1800]
    assert "Закупочная цена TPL" in card
    assert "30.00 GEL" in card
    assert "Оплатить TPL" in card
    # Jinja2 autoescapes "&" to "&amp;" in HTML attribute output.
    assert "https://mpi.gc.ge/page1?merch_id=abc&amp;o.id=xyz" in card

    # The link itself is unchanged by the card-autofill extension work: a
    # plain new-tab link straight to the stored BOG URL -- no iframe, no
    # proxying through our server, and nothing card-related appended to it.
    import re

    href_match = re.search(r'<a href="([^"]+)"[^>]*>Оплатить TPL</a>', card)
    assert href_match, "expected a plain <a href> link for Оплатить TPL"
    assert href_match.group(1) == "https://mpi.gc.ge/page1?merch_id=abc&amp;o.id=xyz"
    assert 'target="_blank"' in card
    assert "<iframe" not in card
    assert "pan=" not in card.lower() and "cvc=" not in card.lower() and "cvv=" not in card.lower()

    # The new operator guidance line is present, making clear the server
    # itself has no role in filling the card.
    assert "Данные карты будут заполнены на компьютере оператора" in card
    assert "Получить новую ссылку" in card
    assert "Оплата TPL завершена" in card
    assert "Ссылка действует ограниченное время" in card


def test_clicking_issue_twice_never_sends_a_second_application(admin_configured, monkeypatch):
    calls = _patch_http_layer(monkeypatch)
    resume_token = _create_paid_order(plate="GEFLOW2")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")  # "Получить новую ссылку"

    assert calls["create_application"] == 1
    assert calls["initiate_bog_payment"] == 2


def test_mark_paid_shows_awaiting_policy_text_without_advancing_order_status(admin_configured, monkeypatch):
    _patch_http_layer(monkeypatch)
    resume_token = _create_paid_order(plate="GEFLOW3")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    response = _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")
    assert response.status_code == 303

    # Still PROCESSING -- never auto-advanced to POLICY_READY.
    assert _order(resume_token).status == OrderStatus.PROCESSING.value

    page = _admin_get(client)
    idx = page.text.find(_order(resume_token).public_number)
    card = page.text[idx : idx + 1200]
    assert "Ожидается получение полиса" in card
    assert "Оплатить TPL" not in card


def test_application_failure_shown_as_operator_facing_error(admin_configured, monkeypatch):
    def _boom(client, payload):
        raise tpl_ge_service.tpl_client.TplPoliciesError("simulated rejection")

    _patch_http_layer(monkeypatch)
    monkeypatch.setattr(tpl_ge_service.tpl_client, "create_application", _boom)
    resume_token = _create_paid_order(plate="GEFAIL1")

    response = _admin_post(TestClient(app), f"/admin/orders/{resume_token}/tpl/issue")
    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.PAID.value  # never advanced

    page = _admin_get(TestClient(app))
    idx = page.text.find(_order(resume_token).public_number)
    card = page.text[idx : idx + 1200]
    assert "Оформить полис TPL" in card  # still offered, not stuck


def test_visitor_id_not_configured_is_a_clear_operator_error(monkeypatch):
    """Deliberately WITHOUT admin_configured's TPL_GE_STATIC_VISITOR_ID --
    admin auth alone (needed just to reach the page) is configured inline."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-test-only")
    get_settings.cache_clear()
    _patch_http_layer(monkeypatch)
    resume_token = _create_paid_order(plate="GENOVIS1")

    response = _admin_post(TestClient(app), f"/admin/orders/{resume_token}/tpl/issue")
    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.PAID.value

    issuance = tpl_repo.get_issuance_by_order_id(get_connection(_settings.app.db_file), _order(resume_token).id)
    assert issuance.is_failed
    assert issuance.last_error
    get_settings.cache_clear()
