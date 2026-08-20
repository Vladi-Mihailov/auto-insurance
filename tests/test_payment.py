"""Manual RUB payment confirmation MVP regression tests.

Covers: the customer "Я оплатил" flow (AWAITING_PAYMENT -> PAYMENT_REVIEW),
admin review (/admin/orders, PAYMENT_REVIEW -> PAID or back to
AWAITING_PAYMENT), HTTP Basic admin auth (fail-closed when unconfigured),
idempotency, price security, and the static QR image.

No new DB table/columns/status enum values -- everything here rides on the
existing insurance_orders.status + insurance_order_status_history (see
app.orders.repository.get_latest_transition_at / list_orders_by_status).

"passenger_car" is re-seeded with the same external_id=7 already used by
test_routes_smoke.py -- upsert_category is a true idempotent upsert, so
this is safe regardless of test collection order.
"""

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import PROJECT_ROOT, get_settings
from app.main import app
from app.orders.repository import get_order_by_token
from app.orders.state_machine import OrderStatus
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
# external_id chosen to not collide with other test files' ranges.
_manufacturer_id = upsert_manufacturer(_conn, external_id=13001, name="ZPAYMENTFICTIONALMAKE", is_popular=True)
_model_id = upsert_model(_conn, external_id=13001, manufacturer_id=_manufacturer_id, name="ZPAYMENTFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


def _create_order_awaiting_payment(client_, plate="PAY001AA"):
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2026-08-20"})
    client_.post("/method", data={"choice": "manual"})
    client_.post(
        "/vehicle",
        data={
            "registration_number": plate,
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000700",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]
    client_.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    return resume_token


def _order(resume_token):
    conn = get_connection(_settings.app.db_file)
    try:
        return get_order_by_token(conn, resume_token)
    finally:
        conn.close()


@pytest.fixture
def admin_configured(monkeypatch):
    """Same pattern as test_routes_smoke.py's real_production_config --
    get_settings() is @lru_cache'd, so the cache must be cleared on both
    sides of the env var change."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret-test-only")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------- 1-2: price security -------------------------------


def test_awaiting_payment_page_renders_exact_order_price():
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_)
    response = client_.get(f"/o/{resume_token}/payment")
    assert response.status_code == 200
    assert "1 500" in response.text  # test fixture pricing, see tests/fixtures/test_config.yaml
    assert _order(resume_token).price_customer_minor == 150000


def test_confirm_payment_cannot_override_amount():
    """The POST carries no amount field at all -- sending one anyway (as if
    a malicious/buggy client tried) must have zero effect on the stored
    price, since the route never reads any such field."""
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_)
    before = _order(resume_token).price_customer_minor

    client_.post(
        f"/o/{resume_token}/confirm-payment",
        data={"amount": "1", "price_customer_minor": "999999999"},
    )

    after = _order(resume_token)
    assert after.price_customer_minor == before


# --------------------------- 3-6: customer confirm-payment flow ----------------


def test_confirm_payment_transitions_to_payment_review():
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_)

    response = client_.post(f"/o/{resume_token}/confirm-payment", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/o/{resume_token}/payment"
    assert _order(resume_token).status == OrderStatus.PAYMENT_REVIEW.value


def test_confirm_payment_writes_a_status_history_row():
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_)
    order_id = _order(resume_token).id

    client_.post(f"/o/{resume_token}/confirm-payment")

    conn = get_connection(_settings.app.db_file)
    try:
        row = conn.execute(
            "SELECT from_status, to_status, note FROM insurance_order_status_history "
            "WHERE order_id = ? ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["from_status"] == "awaiting_payment"
    assert row["to_status"] == "payment_review"
    assert row["note"] == "customer submitted payment confirmation"


def test_repeated_confirm_payment_is_idempotent():
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_)

    first = client_.post(f"/o/{resume_token}/confirm-payment", follow_redirects=False)
    assert first.status_code == 303
    second = client_.post(f"/o/{resume_token}/confirm-payment", follow_redirects=False)
    assert second.status_code == 303  # no 500, no crash

    assert _order(resume_token).status == OrderStatus.PAYMENT_REVIEW.value

    conn = get_connection(_settings.app.db_file)
    try:
        order_id = _order(resume_token).id
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM insurance_order_status_history "
            "WHERE order_id = ? AND from_status = 'awaiting_payment' AND to_status = 'payment_review'",
            (order_id,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert count == 1  # exactly one real transition, not two


def test_payment_review_screen_hides_the_pay_button():
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_)
    client_.post(f"/o/{resume_token}/confirm-payment")

    response = client_.get(f"/o/{resume_token}/payment")
    assert response.status_code == 200
    assert "Оплата проверяется" in response.text
    assert "Я оплатил" not in response.text
    assert 'action="/o/' not in response.text or "confirm-payment" not in response.text


# --------------------------- 7: paid screen -------------------------------------


def test_paid_screen_renders_confirmed_state():
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_)
    client_.post(f"/o/{resume_token}/confirm-payment")

    conn = get_connection(_settings.app.db_file)
    try:
        from app.orders.repository import set_status

        order = get_order_by_token(conn, resume_token)
        set_status(conn, order.id, OrderStatus.PAID, note="admin confirmed payment")
    finally:
        conn.close()

    response = client_.get(f"/o/{resume_token}/payment")
    assert response.status_code == 200
    assert "Оплата подтверждена" in response.text
    assert "Я оплатил" not in response.text


# --------------------------- 8-11: admin auth -----------------------------------


def test_admin_orders_unauthenticated_is_401(admin_configured):
    client_ = TestClient(app)
    response = client_.get("/admin/orders")
    assert response.status_code == 401


def test_admin_orders_wrong_credentials_is_401(admin_configured):
    client_ = TestClient(app)
    response = client_.get("/admin/orders", auth=("admin", "wrong-password"))
    assert response.status_code == 401


def test_admin_orders_correct_credentials_grants_access(admin_configured):
    client_ = TestClient(app)
    response = client_.get("/admin/orders", auth=("admin", "s3cret-test-only"))
    assert response.status_code == 200


def test_admin_orders_fails_closed_when_not_configured():
    """No admin_configured fixture here -- ADMIN_USERNAME/ADMIN_PASSWORD are
    unset by default in the test environment (see conftest.py), which must
    mean admin access is refused outright, never silently made public."""
    client_ = TestClient(app)
    response = client_.get("/admin/orders")
    assert response.status_code == 401

    # Even guessing plausible-looking credentials must not work while
    # unconfigured -- the "not configured" check must run first.
    response = client_.get("/admin/orders", auth=("admin", "admin"))
    assert response.status_code == 401


# --------------------------- 12: admin list scope -------------------------------


def test_admin_list_contains_only_payment_review_orders(admin_configured):
    client_ = TestClient(app)
    awaiting_token = _create_order_awaiting_payment(client_, plate="PAY002BB")
    review_token = _create_order_awaiting_payment(client_, plate="PAY003CC")
    client_.post(f"/o/{review_token}/confirm-payment")

    response = client_.get("/admin/orders", auth=("admin", "s3cret-test-only"))
    assert response.status_code == 200
    review_order = _order(review_token)
    awaiting_order = _order(awaiting_token)
    assert review_order.public_number in response.text
    assert awaiting_order.public_number not in response.text


# --------------------------- 13-16: admin confirm/reject ------------------------


def test_admin_confirm_transitions_to_paid(admin_configured):
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_, plate="PAY004DD")
    client_.post(f"/o/{resume_token}/confirm-payment")

    response = client_.post(
        f"/admin/orders/{resume_token}/confirm", auth=("admin", "s3cret-test-only"), follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/orders"
    assert _order(resume_token).status == OrderStatus.PAID.value


def test_admin_double_confirm_is_safe(admin_configured):
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_, plate="PAY005EE")
    client_.post(f"/o/{resume_token}/confirm-payment")

    first = client_.post(f"/admin/orders/{resume_token}/confirm", auth=("admin", "s3cret-test-only"))
    second = client_.post(f"/admin/orders/{resume_token}/confirm", auth=("admin", "s3cret-test-only"))
    assert first.status_code == 200  # after following the redirect
    assert second.status_code == 200  # no 500 on the second, already-PAID attempt
    assert _order(resume_token).status == OrderStatus.PAID.value


def test_admin_reject_returns_order_to_awaiting_payment(admin_configured):
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_, plate="PAY006FF")
    client_.post(f"/o/{resume_token}/confirm-payment")

    response = client_.post(
        f"/admin/orders/{resume_token}/reject", auth=("admin", "s3cret-test-only"), follow_redirects=False
    )
    assert response.status_code == 303
    assert _order(resume_token).status == OrderStatus.AWAITING_PAYMENT.value


def test_paid_order_cannot_be_rejected_or_rolled_back(admin_configured):
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_, plate="PAY007GG")
    client_.post(f"/o/{resume_token}/confirm-payment")
    client_.post(f"/admin/orders/{resume_token}/confirm", auth=("admin", "s3cret-test-only"))
    assert _order(resume_token).status == OrderStatus.PAID.value

    # A stale admin page (or a second admin) clicking "Оплата не найдена"
    # on an order that's already PAID must never roll it back.
    client_.post(f"/admin/orders/{resume_token}/reject", auth=("admin", "s3cret-test-only"))
    assert _order(resume_token).status == OrderStatus.PAID.value


# --------------------------- 17: admin timestamp source -------------------------


def test_admin_list_timestamp_comes_from_status_history(admin_configured):
    from datetime import datetime

    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_, plate="PAY008HH")
    client_.post(f"/o/{resume_token}/confirm-payment")
    order_id = _order(resume_token).id

    conn = get_connection(_settings.app.db_file)
    try:
        history_row = conn.execute(
            "SELECT created_at FROM insurance_order_status_history "
            "WHERE order_id = ? AND from_status = 'awaiting_payment' AND to_status = 'payment_review' "
            "ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()
    assert history_row is not None
    expected = datetime.fromisoformat(history_row["created_at"]).strftime("%d.%m.%Y %H:%M")

    response = client_.get("/admin/orders", auth=("admin", "s3cret-test-only"))
    assert response.status_code == 200
    assert expected in response.text  # exact rendered timestamp matches the history row, not a cached/new column


# --------------------------- 18-19: static QR -----------------------------------


def test_qr_renders_when_configured(monkeypatch):
    monkeypatch.setenv("PAYMENT_QR_IMAGE_URL", "/static/img/payment-qr-test.png")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY009II")
        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        assert '<img class="payment-qr__image" src="/static/img/payment-qr-test.png"' in response.text
    finally:
        get_settings.cache_clear()


def test_no_broken_qr_image_when_not_configured():
    """Default test environment has no PAYMENT_QR_IMAGE_URL set (see
    conftest.py) -- the page must not render an <img> with an empty/missing
    src at all."""
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_, plate="PAY010JJ")
    response = client_.get(f"/o/{resume_token}/payment")
    assert response.status_code == 200
    assert "payment-qr__image" not in response.text
    assert "<img" not in response.text


def test_committed_payment_qr_asset_is_served_correctly():
    """payment-qr.png (app/web/static/img/payment-qr.png) is a real,
    intentionally committed production asset -- the user's actual public
    payment QR, not a secret. This only checks it exists and is served
    correctly; it never decodes/inspects the QR's payload."""
    qr_path = PROJECT_ROOT / "app" / "web" / "static" / "img" / "payment-qr.png"
    assert qr_path.is_file()

    client_ = TestClient(app)
    response = client_.get("/static/img/payment-qr.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert len(response.content) > 0


# --------------------------- 21-25: direct transfer link ------------------------


def test_transfer_link_button_renders_when_configured(monkeypatch):
    monkeypatch.setenv("PAYMENT_TRANSFER_URL", "https://example.test/pay/fake-local-placeholder")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY012LL")
        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        assert 'href="https://example.test/pay/fake-local-placeholder"' in response.text
        assert "Перейти к оплате" in response.text
        # external link -- safe target/rel, and never inserted as raw/unescaped HTML
        assert 'target="_blank"' in response.text
        assert 'rel="noopener noreferrer"' in response.text
    finally:
        get_settings.cache_clear()


def test_transfer_link_button_absent_when_not_configured():
    """Default test environment has no PAYMENT_TRANSFER_URL set (see
    conftest.py) -- no button, no dangling/empty href."""
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_, plate="PAY013MM")
    response = client_.get(f"/o/{resume_token}/payment")
    assert response.status_code == 200
    assert "Перейти к оплате" not in response.text


def test_qr_and_transfer_link_both_render_when_both_configured(monkeypatch):
    monkeypatch.setenv("PAYMENT_QR_IMAGE_URL", "/static/img/payment-qr-test.png")
    monkeypatch.setenv("PAYMENT_TRANSFER_URL", "https://example.test/pay/fake-local-placeholder")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY014NN")
        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        assert '<img class="payment-qr__image" src="/static/img/payment-qr-test.png"' in response.text
        assert "Перейти к оплате" in response.text
    finally:
        get_settings.cache_clear()


def test_transfer_url_visible_as_text_link_under_qr(monkeypatch):
    """A second, distinct link right under the QR that shows the raw URL
    as visible text (not just as an href on the "Перейти к оплате"
    button) -- uses the existing payment.transfer_url value, no new env
    var, no hardcoded URL. Placed as the QR image's next sibling in the
    template (see payment.html), so it renders immediately under it."""
    monkeypatch.setenv("PAYMENT_QR_IMAGE_URL", "/static/img/payment-qr-test.png")
    monkeypatch.setenv("PAYMENT_TRANSFER_URL", "https://example.test/pay/fake-local-placeholder")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY019SS")
        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        # the raw URL is visible TEXT, not just an href attribute
        assert (
            '<a href="https://example.test/pay/fake-local-placeholder" target="_blank" rel="noopener noreferrer" '
            'class="payment-transfer-url">https://example.test/pay/fake-local-placeholder</a>' in response.text
        )
        # it appears after the QR image, not before it (i.e. "under" it)
        qr_pos = response.text.index("payment-qr__image")
        link_pos = response.text.index("payment-transfer-url")
        assert qr_pos < link_pos
        # the big "Перейти к оплате" button stays below the new text link
        button_pos = response.text.index("Перейти к оплате")
        assert link_pos < button_pos
    finally:
        get_settings.cache_clear()


def test_transfer_url_text_link_absent_when_transfer_url_not_configured(monkeypatch):
    """QR alone, no transfer_url -- no text link, no dangling empty href."""
    monkeypatch.setenv("PAYMENT_QR_IMAGE_URL", "/static/img/payment-qr-test.png")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY020TT")
        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        assert "payment-qr__image" in response.text
        assert "payment-transfer-url" not in response.text
    finally:
        get_settings.cache_clear()


def test_awaiting_payment_shows_payment_controls(monkeypatch):
    monkeypatch.setenv("PAYMENT_QR_IMAGE_URL", "/static/img/payment-qr-test.png")
    monkeypatch.setenv("PAYMENT_TRANSFER_URL", "https://example.test/pay/fake-local-placeholder")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY015OO")
        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        assert "payment-qr__image" in response.text
        assert "Перейти к оплате" in response.text
        assert "Я оплатил" in response.text
        assert "card-number" not in response.text  # card block removed from UI
    finally:
        get_settings.cache_clear()


def test_awaiting_payment_never_renders_the_card_block(monkeypatch):
    """The card block (bank name/card number/holder/copy button) is fully
    removed from the UI -- QR + transfer link are the only payment
    methods shown now (see the OCR/checkout task report's PAYMENT
    section). PaymentSettings.bank_name/card_number/card_holder
    themselves are untouched in settings/config -- only the template
    markup that used to render them is gone."""
    monkeypatch.setenv("PAYMENT_QR_IMAGE_URL", "/static/img/payment-qr-test.png")
    monkeypatch.setenv("PAYMENT_TRANSFER_URL", "https://example.test/pay/fake-local-placeholder")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY018RR")
        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        assert "payment-card" not in response.text
        assert "Скопировать номер карты" not in response.text
        assert _settings.payment.card_number not in response.text
        assert _settings.payment.card_holder not in response.text
    finally:
        get_settings.cache_clear()


def test_payment_review_hides_qr_link_and_card(monkeypatch):
    monkeypatch.setenv("PAYMENT_QR_IMAGE_URL", "/static/img/payment-qr-test.png")
    monkeypatch.setenv("PAYMENT_TRANSFER_URL", "https://example.test/pay/fake-local-placeholder")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY016PP")
        client_.post(f"/o/{resume_token}/confirm-payment")

        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        assert "Оплата проверяется" in response.text
        assert "payment-qr__image" not in response.text
        assert "Перейти к оплате" not in response.text
        assert "payment-transfer-url" not in response.text
        assert "card-number" not in response.text
    finally:
        get_settings.cache_clear()


def test_paid_hides_qr_link_and_card(monkeypatch):
    monkeypatch.setenv("PAYMENT_QR_IMAGE_URL", "/static/img/payment-qr-test.png")
    monkeypatch.setenv("PAYMENT_TRANSFER_URL", "https://example.test/pay/fake-local-placeholder")
    get_settings.cache_clear()
    try:
        client_ = TestClient(app)
        resume_token = _create_order_awaiting_payment(client_, plate="PAY017QQ")
        client_.post(f"/o/{resume_token}/confirm-payment")

        conn = get_connection(_settings.app.db_file)
        try:
            from app.orders.repository import set_status

            order = get_order_by_token(conn, resume_token)
            set_status(conn, order.id, OrderStatus.PAID, note="admin confirmed payment")
        finally:
            conn.close()

        response = client_.get(f"/o/{resume_token}/payment")
        assert response.status_code == 200
        assert "Оплата подтверждена" in response.text
        assert "payment-qr__image" not in response.text
        assert "Перейти к оплате" not in response.text
        assert "payment-transfer-url" not in response.text
        assert "card-number" not in response.text
    finally:
        get_settings.cache_clear()


# --------------------------- 20: legacy orders ----------------------------------


def test_legacy_awaiting_payment_order_still_renders_payment_page():
    """Simulates a pre-existing order created before this feature: same
    schema, status already awaiting_payment, no confirm-payment ever
    called. Must render exactly like a freshly created one."""
    client_ = TestClient(app)
    resume_token = _create_order_awaiting_payment(client_, plate="PAY011KK")
    response = client_.get(f"/o/{resume_token}/payment")
    assert response.status_code == 200
    assert "Я оплатил" in response.text
    assert "Оплата проверяется" not in response.text
    assert "Оплата подтверждена" not in response.text
