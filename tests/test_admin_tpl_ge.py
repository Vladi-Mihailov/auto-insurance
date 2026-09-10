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
from app.notifications import telegram as telegram_module
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
    # Never actually sleep in tests -- retrieve_issued_policy_with_retry's
    # bounded retry delays are real seconds in production, not something
    # any test should wait through (same convention as
    # tests/test_catalog_sync.py monkeypatching sync.time.sleep).
    monkeypatch.setattr(tpl_ge_service.time, "sleep", lambda seconds: None)
    return calls


_ISSUED_POLICY_RESPONSE = {
    "policyNumber": "TPL7635945",
    "policyId": 1234567,
    "documents": [{"file": "policy-TPL7635945.pdf", "url": "https://ext-stream.tpl.ge/policies//abc/policy-TPL7635945.pdf"}],
}
_DETAILED_DOCUMENTS_RESPONSE = [
    {"documentType": "Policy", "file": "policy-TPL7635945.pdf", "url": "https://ext-stream.tpl.ge/policies//abc/policy-TPL7635945.pdf"},
    {"documentType": "Invoice", "file": "invoice-TPL7635945.pdf", "url": "https://ext-stream.tpl.ge/policies//abc/invoice-TPL7635945.pdf"},
]
_NOT_READY_POLICY_RESPONSE = {"policyNumber": None, "documents": []}


def _patch_policy_lookup(monkeypatch, *, policy=None, documents=None, fetch_policy_error=None):
    calls = {"fetch_policy": 0, "fetch_policy_documents": 0}

    def _fetch_policy(client, o_id):
        calls["fetch_policy"] += 1
        if fetch_policy_error:
            raise fetch_policy_error
        return policy

    def _fetch_documents(client, o_id):
        calls["fetch_policy_documents"] += 1
        return documents

    monkeypatch.setattr(tpl_ge_service.tpl_client, "fetch_policy", _fetch_policy)
    monkeypatch.setattr(tpl_ge_service.tpl_client, "fetch_policy_documents", _fetch_documents)
    return calls


def _patch_pdf_download(monkeypatch, *, pdf_bytes=b"%PDF-1.4 fake policy pdf", error=None):
    calls = []

    def _download(client, url):
        calls.append(url)
        if error:
            raise error
        return pdf_bytes

    monkeypatch.setattr(tpl_ge_service.tpl_client, "download_document", _download)
    return calls


def _patch_telegram_document(monkeypatch, *, succeed=True):
    """Patches at the telegram module's OWN function boundary (not the name
    admin_routes imported) -- notify_operator_policy_ready resolves
    send_document via telegram.py's own globals at call time, so patching
    it here affects every caller regardless of which module imported
    notify_operator_policy_ready itself (same convention already used by
    tests/test_operator_notifications.py for send_message)."""
    calls = []

    def _send_document(**kwargs):
        calls.append(kwargs)
        if not succeed:
            raise telegram_module.TelegramNotifyError("simulated Telegram outage")

    monkeypatch.setattr(telegram_module, "send_document", _send_document)
    return calls


def _telegram_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-api-hash-not-real")
    monkeypatch.setenv("TELEGRAM_PHONE", "+10000000000")
    monkeypatch.setenv("TELEGRAM_OPERATOR_CHAT_ID", "-5535243432")
    get_settings.cache_clear()


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


def test_mark_paid_not_ready_shows_forming_text_without_advancing_order_status(admin_configured, monkeypatch):
    _patch_http_layer(monkeypatch)
    _patch_policy_lookup(monkeypatch, policy=_NOT_READY_POLICY_RESPONSE, documents=[])
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
    assert "Полис TPL ещё формируется" in card
    assert "Получить полис повторно" in card
    assert "Оплатить TPL" not in card


# ---------------------------------------------------------------------------
# Policy retrieval + Telegram PDF delivery
# ---------------------------------------------------------------------------


def test_mark_paid_immediate_ready_persists_and_delivers_pdf(admin_configured, monkeypatch):
    _patch_http_layer(monkeypatch)
    _patch_policy_lookup(monkeypatch, policy=_ISSUED_POLICY_RESPONSE, documents=_DETAILED_DOCUMENTS_RESPONSE)
    pdf_calls = _patch_pdf_download(monkeypatch)
    telegram_calls = _patch_telegram_document(monkeypatch)
    _telegram_configured(monkeypatch)
    resume_token = _create_paid_order(plate="GEREADY1")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")

    issuance = tpl_repo.get_issuance_by_order_id(get_connection(_settings.app.db_file), _order(resume_token).id)
    assert issuance.policy_number == "TPL7635945"
    assert issuance.is_policy_retrieved
    assert issuance.is_sent_to_operator

    assert pdf_calls == ["https://ext-stream.tpl.ge/policies//abc/policy-TPL7635945.pdf"]  # Policy PDF only
    assert len(telegram_calls) == 1
    sent = telegram_calls[0]
    assert sent["filename"] == "policy-TPL7635945.pdf"
    assert "TPL7635945" in sent["caption"]
    assert sent["file_bytes"] == b"%PDF-1.4 fake policy pdf"
    assert sent["chat_id"] == get_settings().telegram_operator.chat_id


def test_mark_paid_shows_issued_policy_number_in_admin(admin_configured, monkeypatch):
    _patch_http_layer(monkeypatch)
    _patch_policy_lookup(monkeypatch, policy=_ISSUED_POLICY_RESPONSE, documents=_DETAILED_DOCUMENTS_RESPONSE)
    _patch_pdf_download(monkeypatch)
    _patch_telegram_document(monkeypatch)
    _telegram_configured(monkeypatch)
    resume_token = _create_paid_order(plate="GEREADY2")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")

    page = _admin_get(client)
    idx = page.text.find(_order(resume_token).public_number)
    card = page.text[idx : idx + 1200]
    assert "✅ Полис оформлен" in card
    assert "TPL7635945" in card


def test_mark_paid_not_ready_then_ready_via_retry(admin_configured, monkeypatch):
    """PolicyNotReadyError on the first attempt(s), issued on a later one --
    all within the SAME bounded retry window of a single click."""
    _patch_http_layer(monkeypatch)
    attempts = {"n": 0}

    def _fetch_policy(client, o_id):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return _NOT_READY_POLICY_RESPONSE
        return _ISSUED_POLICY_RESPONSE

    monkeypatch.setattr(tpl_ge_service.tpl_client, "fetch_policy", _fetch_policy)
    monkeypatch.setattr(tpl_ge_service.tpl_client, "fetch_policy_documents", lambda client, o_id: _DETAILED_DOCUMENTS_RESPONSE)
    _patch_pdf_download(monkeypatch)
    _patch_telegram_document(monkeypatch)
    _telegram_configured(monkeypatch)
    resume_token = _create_paid_order(plate="GERETRY1")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")

    assert attempts["n"] == 3
    issuance = tpl_repo.get_issuance_by_order_id(get_connection(_settings.app.db_file), _order(resume_token).id)
    assert issuance.is_policy_retrieved
    assert issuance.is_sent_to_operator


def test_mark_paid_not_ready_after_all_retries_is_non_fatal(admin_configured, monkeypatch):
    _patch_http_layer(monkeypatch)
    _patch_policy_lookup(monkeypatch, policy=_NOT_READY_POLICY_RESPONSE, documents=[])
    resume_token = _create_paid_order(plate="GERETRY2")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    response = _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")

    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.PROCESSING.value  # order not lost/broken
    issuance = tpl_repo.get_issuance_by_order_id(get_connection(_settings.app.db_file), _order(resume_token).id)
    assert not issuance.is_policy_retrieved
    assert issuance.is_operator_reported_paid  # acknowledgement itself still stuck


def test_mark_paid_temporary_tpl_error_does_not_lose_order(admin_configured, monkeypatch):
    _patch_http_layer(monkeypatch)
    _patch_policy_lookup(
        monkeypatch, fetch_policy_error=tpl_ge_service.tpl_client.PolicyLookupHttpError("simulated 500")
    )
    resume_token = _create_paid_order(plate="GEERR1")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    response = _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")

    assert response.status_code == 303
    order = _order(resume_token)
    assert order.status == OrderStatus.PROCESSING.value
    issuance = tpl_repo.get_issuance_by_order_id(get_connection(_settings.app.db_file), order.id)
    assert not issuance.is_policy_retrieved
    assert issuance.is_operator_reported_paid  # still safely retryable, nothing corrupted
    assert issuance.last_error


def test_only_policy_pdf_is_downloaded_never_invoice_or_additional_terms(admin_configured, monkeypatch):
    _patch_http_layer(monkeypatch)
    _patch_policy_lookup(monkeypatch, policy=_ISSUED_POLICY_RESPONSE, documents=_DETAILED_DOCUMENTS_RESPONSE)
    pdf_calls = _patch_pdf_download(monkeypatch)
    _patch_telegram_document(monkeypatch)
    _telegram_configured(monkeypatch)
    resume_token = _create_paid_order(plate="GEONLYPOLICY1")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")

    assert len(pdf_calls) == 1
    assert "policy-" in pdf_calls[0]
    assert "invoice" not in pdf_calls[0].lower()
    assert "additionalterms" not in pdf_calls[0].lower()


def test_policy_persisted_even_when_telegram_delivery_fails(admin_configured, monkeypatch):
    """Policy issuance and Telegram delivery are two different outcomes --
    a Telegram failure must never lose the already-retrieved policy."""
    _patch_http_layer(monkeypatch)
    _patch_policy_lookup(monkeypatch, policy=_ISSUED_POLICY_RESPONSE, documents=_DETAILED_DOCUMENTS_RESPONSE)
    _patch_pdf_download(monkeypatch)
    _patch_telegram_document(monkeypatch, succeed=False)
    _telegram_configured(monkeypatch)
    resume_token = _create_paid_order(plate="GETGFAIL1")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    response = _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")
    assert response.status_code == 303

    issuance = tpl_repo.get_issuance_by_order_id(get_connection(_settings.app.db_file), _order(resume_token).id)
    assert issuance.policy_number == "TPL7635945"  # not lost
    assert issuance.policy_document_url  # not lost
    assert issuance.is_policy_retrieved  # not lost
    assert not issuance.is_sent_to_operator
    assert issuance.last_error


def test_telegram_resend_after_earlier_failure(admin_configured, monkeypatch):
    """A later click ("Отправить повторно"/the same mark-paid action) must
    be able to deliver the PDF once Telegram is healthy again -- the policy
    itself is never re-fetched from TPL a second time."""
    _patch_http_layer(monkeypatch)
    fetch_calls = _patch_policy_lookup(monkeypatch, policy=_ISSUED_POLICY_RESPONSE, documents=_DETAILED_DOCUMENTS_RESPONSE)
    _patch_pdf_download(monkeypatch)
    _patch_telegram_document(monkeypatch, succeed=False)
    _telegram_configured(monkeypatch)
    resume_token = _create_paid_order(plate="GERESEND1")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")  # retrieval OK, Telegram fails

    telegram_calls = _patch_telegram_document(monkeypatch, succeed=True)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")  # resend

    assert fetch_calls["fetch_policy"] == 1  # never re-fetched from TPL
    assert len(telegram_calls) == 1  # delivered exactly once on the resend
    issuance = tpl_repo.get_issuance_by_order_id(get_connection(_settings.app.db_file), _order(resume_token).id)
    assert issuance.is_sent_to_operator


def test_duplicate_telegram_delivery_prevented_on_repeat_click(admin_configured, monkeypatch):
    _patch_http_layer(monkeypatch)
    _patch_policy_lookup(monkeypatch, policy=_ISSUED_POLICY_RESPONSE, documents=_DETAILED_DOCUMENTS_RESPONSE)
    _patch_pdf_download(monkeypatch)
    telegram_calls = _patch_telegram_document(monkeypatch)
    _telegram_configured(monkeypatch)
    resume_token = _create_paid_order(plate="GENODUP1")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")  # repeat click ("Получить полис повторно")
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")  # and again

    assert len(telegram_calls) == 1  # never sent twice


def test_repeated_mark_paid_never_creates_another_application_or_payment(admin_configured, monkeypatch):
    calls = _patch_http_layer(monkeypatch)
    _patch_policy_lookup(monkeypatch, policy=_ISSUED_POLICY_RESPONSE, documents=_DETAILED_DOCUMENTS_RESPONSE)
    _patch_pdf_download(monkeypatch)
    _patch_telegram_document(monkeypatch)
    _telegram_configured(monkeypatch)
    resume_token = _create_paid_order(plate="GENOAPP1")

    client = TestClient(app)
    _admin_post(client, f"/admin/orders/{resume_token}/tpl/issue")
    for _ in range(3):
        _admin_post(client, f"/admin/orders/{resume_token}/tpl/mark-paid")

    assert calls["create_application"] == 1
    assert calls["initiate_bog_payment"] == 1  # only the one from /tpl/issue -- never re-initiated


def test_existing_bog_flow_unchanged_by_policy_retrieval_work(admin_configured, monkeypatch):
    """The pre-existing "Оформить полис TPL"/"Получить новую ссылку" flow
    (issue_tpl_policy) behaves identically to before this task -- policy
    retrieval is only ever reached via /tpl/mark-paid."""
    calls = _patch_http_layer(monkeypatch)
    resume_token = _create_paid_order(plate="GEBOGSAME1")

    response = _admin_post(TestClient(app), f"/admin/orders/{resume_token}/tpl/issue")
    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.PROCESSING.value
    assert calls["create_application"] == 1
    assert calls["initiate_bog_payment"] == 1

    issuance = tpl_repo.get_issuance_by_order_id(get_connection(_settings.app.db_file), _order(resume_token).id)
    assert issuance.is_bog_link_ready
    assert not issuance.is_operator_reported_paid
    assert not issuance.is_policy_retrieved


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
