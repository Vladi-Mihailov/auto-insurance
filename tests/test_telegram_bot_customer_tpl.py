"""A NORMAL customer order converges on the same tpl.ge issuance as an
operator order: receipt -> manager "✅ Оплата поступила" -> (committed and
acknowledged) start_tpl_issuance -> BOG link for the manager -> "✅ Оплата
TPL завершена" -> policy -> PDF to THE CUSTOMER.

Never a real tpl.ge / BOG call (FakeTpl from the operator tests replaces
every one at the module-function boundary). Fake Bot API, tmp DB."""

import httpx
import pytest
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from aiogram.types import BufferedInputFile

from app.deps import PROJECT_ROOT
from app.integrations.tpl_ge import service as tpl_service
from app.orders.state_machine import OrderStatus
from app.settings import load_settings
from app.telegram_bot import texts
from telegram_bot_helpers import BotHarness, buttons, last_screen, make_user, screens, seed_catalog, sent_to
from test_telegram_bot_operator_issue import (
    BOG_URL,
    ISSUED,
    OWNER,
    PROFILE,
    VIN,
    FakeTpl,
    Provider,
    _conn,
    _history,
    _issuance,
    _order,
    _orders,
    _review,
)

CUSTOMER_ID = 300


def _env(monkeypatch, tmp_path, *, visitor_id: bool):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+79495205223")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Владимир М.")
    # This whole module is the AUTOMATIC issuance (off by default, see
    # tests/test_telegram_bot_manual_policy.py for the default manual flow).
    monkeypatch.setenv("TELEGRAM_TPL_AUTO_ISSUANCE", "true")
    if visitor_id:
        monkeypatch.setenv("TPL_GE_STATIC_VISITOR_ID", "test-visitor-id")
    else:
        monkeypatch.delenv("TPL_GE_STATIC_VISITOR_ID", raising=False)
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    return _env(monkeypatch, tmp_path, visitor_id=True)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


@pytest.fixture
def tpl(monkeypatch):
    return FakeTpl().install(monkeypatch)


@pytest.fixture
def h(settings, ids, tpl):
    harness = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    yield harness
    harness.close()


def _paid_review(h, customer=None):
    """A customer's order up to PAYMENT_REVIEW (receipt uploaded)."""
    customer = customer or make_user(CUSTOMER_ID, "client")
    _review(h, customer)
    h.press(customer, "fc:confirm")
    order = _orders(h.dispatcher["settings"], customer.id)[0]
    h.send_photo(customer, b"receipt", file_id=f"rcpt-{customer.id}")
    return customer, order


def _status_messages(calls, chat_id):
    return [c for c in sent_to(calls, chat_id, SendMessage) if c.text.startswith(("🛡", "⚠️", "❌", "⏳", "✅ Оплата клиента"))]


# ------------------------------------------------------------ HAPPY PATH


def test_confirmed_customer_payment_goes_through_tpl_and_the_pdf_reaches_the_customer(h, settings, ids, tpl):
    customer, order = _paid_review(h)
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value and tpl.applications == []

    calls = h.press(make_user(OWNER), f"mg:confirm:{order.id}")
    # our payment confirmation first (and the customer is told as before) ...
    assert [c.text.split("\n")[0] for c in sent_to(calls, CUSTOMER_ID, SendMessage)] == ["✅ Оплата подтверждена"]
    assert ("payment_review", "paid") in [(a, b) for a, b, _ in _history(settings, order.id)]
    # ... then the SAME tpl.ge issuance, with this order's data
    (payload,) = tpl.applications
    assert (payload["insurerTitle"], payload["insurerIdentificationNumber"], payload["vinCode"]) == ("PETROV PETR", "751234567", VIN)
    assert payload["vehicleRegistrationNumber"] == "AB123CD" and payload["insurerEmail"] == "tplgee@mail.ru"
    (status,) = _status_messages(calls, OWNER)
    assert status.text.startswith("🛡 Заявка создана в tpl.ge") and f"Заказ {order.public_number} · оплачен клиентом" in status.text
    assert status.reply_markup.inline_keyboard[0][0].url == BOG_URL
    assert [t for t, _ in buttons(status)] == ["💳 Оплатить в tpl.ge", "✅ Оплата TPL завершена", "🔄 Новая ссылка на оплату", "⬅️ К заказу"]
    assert _order(settings, order.id).status == OrderStatus.PROCESSING.value
    assert sent_to(calls, CUSTOMER_ID, SendDocument) == []  # nothing for the customer yet

    done = h.press(make_user(OWNER), f"op:paid:{order.id}")
    (pdf,) = sent_to(done, CUSTOMER_ID, SendDocument)  # THE CUSTOMER gets the policy
    assert isinstance(pdf.document, BufferedInputFile) and pdf.document.data == b"%PDF-1.4 the real policy"
    assert pdf.caption == texts.POLICY_CAPTION.format(number=order.public_number)
    assert sent_to(done, OWNER, SendDocument) == []  # not the confirming manager
    assert last_screen(done).text.startswith("🎉 Полис TPL7635945 оформлен и отправлен клиенту.")
    assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value
    assert _issuance(settings, order.id).policy_number == "TPL7635945"
    # the managers' tracked cards follow, and the customer can get the PDF again as usual
    assert any(getattr(c, "chat_id", None) == OWNER and "📄 Полис отправлен" in getattr(c, "text", "") for c in done)
    again = h.press(customer, f"o:resend_policy:{order.id}")
    assert len(sent_to(again, CUSTOMER_ID, SendDocument)) == 1


def test_another_manager_confirming_still_sends_the_pdf_to_the_customer(settings, ids, tpl):
    h = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    try:
        screen = last_screen(h.press(make_user(OWNER), "st:invite:0:0"))
        manager = make_user(77, "anna")
        h.send_text(manager, f"/start {screen.text.split('start=', 1)[1].split()[0]}")
        customer, order = _paid_review(h)
        calls = h.press(manager, f"mg:confirm:{order.id}")
        assert len(_status_messages(calls, 77)) == 1  # the confirming manager is guided
        done = h.press(make_user(OWNER), f"op:paid:{order.id}")
        assert len(sent_to(done, CUSTOMER_ID, SendDocument)) == 1
        assert sent_to(done, 77, SendDocument) == [] and sent_to(done, OWNER, SendDocument) == []
    finally:
        h.close()


# ------------------------------------------------------------ IDEMPOTENCY


def test_duplicate_confirmation_and_issuance_presses_make_one_application(h, settings, ids, tpl):
    _, order = _paid_review(h)
    h.press(make_user(OWNER), f"mg:confirm:{order.id}")
    second = h.press(make_user(OWNER), f"mg:confirm:{order.id}")
    assert [c.text for c in second if isinstance(c, AnswerCallbackQuery)][0] == texts.MGR_ALREADY_DONE
    bog = tpl.bog_calls
    for _ in range(2):
        h.press(make_user(OWNER), f"op:start:{order.id}")
        h.press(make_user(OWNER), f"op:retry:{order.id}")
    assert len(tpl.applications) == 1 and tpl.bog_calls == bog  # no second application, not even a new link
    assert len(_orders(settings, CUSTOMER_ID)) == 1


def test_restart_resumes_without_a_second_application(settings, ids, tpl):
    first = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    _, order = _paid_review(first)
    first.press(make_user(OWNER), f"mg:confirm:{order.id}")
    first.close()
    second = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    try:
        card = last_screen(second.press(make_user(OWNER), f"st:co:{order.id}:0"))
        resumed = last_screen(second.press(make_user(OWNER), dict((t, d) for t, d in buttons(card))["🛡 Оформить в tpl.ge"]))
        assert resumed.text.startswith("🛡 Заявка создана в tpl.ge") and len(tpl.applications) == 1
        assert len(sent_to(second.press(make_user(OWNER), f"op:paid:{order.id}"), CUSTOMER_ID, SendDocument)) == 1
    finally:
        second.close()


# --------------------------------------------------------- NOT CONFIGURED


def test_missing_visitor_id_fails_safe_and_the_paid_order_resumes_later(tmp_path, monkeypatch, tpl):
    unconfigured = _env(monkeypatch, tmp_path, visitor_id=False)
    seed_catalog(unconfigured.app.db_file)
    h = BotHarness(unconfigured, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    try:
        _, order = _paid_review(h)
        calls = h.press(make_user(OWNER), f"mg:confirm:{order.id}")
        assert _order(unconfigured, order.id).status == OrderStatus.PAID.value  # our confirmation stands
        assert [c.text.split("\n")[0] for c in sent_to(calls, CUSTOMER_ID, SendMessage)] == ["✅ Оплата подтверждена"]
        (status,) = _status_messages(calls, OWNER)
        assert status.text.startswith("⚠️ Автоматическое оформление в tpl.ge не настроено")
        assert [t for t, _ in buttons(status)] == ["🔁 Повторить оформление", "📎 Загрузить полис вручную", "⬅️ К заказу"]
        assert _issuance(unconfigured, order.id) is None  # nothing at all was attempted
        assert tpl.applications == [] and tpl.bog_calls == 0
        # exactly ORDER-1082's state: paid customer order, no issuance; the card offers the resume
        card = last_screen(h.press(make_user(OWNER), f"st:co:{order.id}:0"))
        assert [t for t, _ in buttons(card)][:2] == ["🛡 Оформить в tpl.ge", "📎 Загрузить полис вручную"]
    finally:
        h.close()

    configured = _env(monkeypatch, tmp_path, visitor_id=True)  # configured, bot restarted
    h = BotHarness(configured, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    try:
        resumed = last_screen(h.press(make_user(OWNER), f"op:start:{order.id}"))
        assert resumed.text.startswith("🛡 Заявка создана в tpl.ge") and len(tpl.applications) == 1
        # no new receipt, no second confirmation, no new order
        assert len(_orders(configured, CUSTOMER_ID)) == 1
        history = [(a, b) for a, b, _ in _history(configured, order.id)]
        assert history.count(("payment_review", "paid")) == 1 and history.count(("awaiting_payment", "payment_review")) == 1
    finally:
        h.close()


# --------------------------------------------------------------- FAILURES


def test_tpl_rejection_keeps_the_paid_order_and_offers_retry_and_manual_fallback(h, settings, ids, tpl):
    _, order = _paid_review(h)
    tpl.create_error = tpl_service.tpl_client.TplPoliciesError("POST /api/policies -> 400")
    calls = h.press(make_user(OWNER), f"mg:confirm:{order.id}")
    (status,) = _status_messages(calls, OWNER)
    assert status.text.startswith("❌ tpl.ge отклонил заявку")
    labels = [t for t, _ in buttons(status)]
    assert labels == ["🔁 Повторить оформление", "📎 Загрузить полис вручную", "⬅️ К заказу"]  # no draft editing for a customer order
    assert _order(settings, order.id).status == OrderStatus.PAID.value
    tpl.create_error = None
    assert last_screen(h.press(make_user(OWNER), f"op:retry:{order.id}")).text.startswith("🛡 Заявка создана в tpl.ge")
    assert len(tpl.applications) == 1


def test_temporary_failure_then_retry(h, settings, ids, tpl):
    _, order = _paid_review(h)
    tpl.categories_error = httpx.ConnectError("tpl.ge down")
    calls = h.press(make_user(OWNER), f"mg:confirm:{order.id}")
    assert _status_messages(calls, OWNER)[0].text.startswith("⚠️ Временная ошибка связи с tpl.ge")
    assert _order(settings, order.id).status == OrderStatus.PAID.value and tpl.applications == []
    tpl.categories_error = None
    h.press(make_user(OWNER), f"op:retry:{order.id}")
    assert len(tpl.applications) == 1


def test_unknown_application_outcome_is_never_resent(h, settings, ids, tpl):
    _, order = _paid_review(h)
    tpl.create_error = httpx.ReadTimeout("no answer")
    calls = h.press(make_user(OWNER), f"mg:confirm:{order.id}")
    assert _status_messages(calls, OWNER)[0].text.startswith("⏳ Заявка отправлена в tpl.ge, но ответ не получен")
    tpl.create_error = None
    h.press(make_user(OWNER), f"op:retry:{order.id}")
    h.press(make_user(OWNER), f"op:start:{order.id}")
    assert tpl.applications == [] and _issuance(settings, order.id).is_application_requested


def test_bog_link_refresh_only_before_tpl_payment(h, settings, ids, tpl):
    _, order = _paid_review(h)
    h.press(make_user(OWNER), f"mg:confirm:{order.id}")
    before = tpl.bog_calls
    refreshed = last_screen(h.press(make_user(OWNER), f"op:link:{order.id}"))
    assert tpl.bog_calls == before + 1 and refreshed.text.startswith("🛡 Заявка создана в tpl.ge")
    tpl.policy = {"policyNumber": None, "documents": []}
    forming = last_screen(h.press(make_user(OWNER), f"op:paid:{order.id}"))
    assert forming.text.startswith("⏳ Оплата отмечена — полис ещё формируется")
    h.press(make_user(OWNER), f"op:link:{order.id}")  # after the TPL payment: never a new link
    assert tpl.bog_calls == before + 1
    tpl.policy = ISSUED
    assert len(sent_to(h.press(make_user(OWNER), f"op:paid:{order.id}"), CUSTOMER_ID, SendDocument)) == 1
    assert len(tpl.applications) == 1


# ---------------------------------------------------------- AUTHORIZATION


def test_customers_cannot_start_issuance_and_unpaid_orders_cannot_be_issued(h, settings, ids, tpl):
    customer, order = _paid_review(h)
    for data in (f"op:start:{order.id}", f"op:retry:{order.id}", f"op:paid:{order.id}", f"op:link:{order.id}"):
        calls = h.press(customer, data)
        assert [c.text for c in calls if isinstance(c, AnswerCallbackQuery)] == [texts.MGR_NO_ACCESS]
        assert not screens(calls)
    # a manager can't issue before OUR payment is confirmed (payment_review here)
    screen = last_screen(h.press(make_user(OWNER), f"op:start:{order.id}"))
    assert screen.text.startswith("Оплата клиента по этому заказу ещё не подтверждена")
    assert tpl.applications == []
