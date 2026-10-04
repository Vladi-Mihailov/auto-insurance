"""The DEFAULT customer-order flow while automatic tpl.ge issuance is off
(TELEGRAM_TPL_AUTO_ISSUANCE unset/false): receipt -> manager "✅ Оплата
поступила" -> order stays PAID, customer told it went for processing ->
"📄 Загрузить готовый полис" -> the manager's PDF -> THE CUSTOMER.

A confirmed customer payment must never reach tpl.ge: every tpl.ge entry
point is a tripwire here. The automatic issuance itself is covered (flag on)
by tests/test_telegram_bot_customer_tpl.py. Fake Bot API, tmp DB."""

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage

from app.deps import PROJECT_ROOT
from app.integrations.tpl_ge import service as tpl_service
from app.orders.state_machine import OrderStatus
from app.settings import load_settings
from app.telegram_bot import texts
from telegram_bot_helpers import BotHarness, buttons, last_screen, make_user, seed_catalog, sent_to
from test_telegram_bot_operator_issue import OWNER, PROFILE, FakeTpl, Provider, _history, _order, _orders, _review

CUSTOMER_ID = 400


def _env(monkeypatch, tmp_path, *, auto: bool | None = None):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+79495205223")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Владимир М.")
    # Even a CONFIGURED tpl.ge must not be used while the flag is off.
    monkeypatch.setenv("TPL_GE_STATIC_VISITOR_ID", "test-visitor-id")
    if auto is None:
        monkeypatch.delenv("TELEGRAM_TPL_AUTO_ISSUANCE", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_TPL_AUTO_ISSUANCE", "true" if auto else "false")
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    return _env(monkeypatch, tmp_path)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


@pytest.fixture
def tpl(monkeypatch):
    """FakeTpl records any application; on top of it, the service's own
    entry points are tripwires -- the manual flow must never get there."""
    fake = FakeTpl().install(monkeypatch)

    def tripwire(*args, **kwargs):
        raise AssertionError("tpl.ge must not be called while automatic issuance is off")

    for name in ("issue_tpl_policy", "report_operator_paid", "retrieve_issued_policy_with_retry", "download_policy_pdf"):
        monkeypatch.setattr(tpl_service, name, tripwire)
    return fake


@pytest.fixture
def h(settings, ids, tpl):
    harness = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    yield harness
    harness.close()


def _paid_review(h, customer=None):
    customer = customer or make_user(CUSTOMER_ID, "client")
    _review(h, customer)
    h.press(customer, "fc:confirm")
    order = _orders(h.dispatcher["settings"], customer.id)[0]
    h.send_photo(customer, b"receipt", file_id=f"rcpt-{customer.id}")
    return customer, order


def _answers(calls) -> list[str]:
    return [c.text for c in calls if isinstance(c, AnswerCallbackQuery) and c.text]


def test_flag_defaults_to_off(settings):
    assert settings.telegram_bot.tpl_auto_issuance is False


def test_confirmation_keeps_paid_tells_the_customer_and_never_calls_tpl(h, settings, ids, tpl):
    _, order = _paid_review(h)

    calls = h.press(make_user(OWNER), f"mg:confirm:{order.id}")

    assert _order(settings, order.id).status == OrderStatus.PAID.value
    assert tpl.applications == []
    (notice,) = sent_to(calls, CUSTOMER_ID, SendMessage)
    assert notice.text == texts.PAYMENT_CONFIRMED.format(number=order.public_number)
    assert "передан в оформление" in notice.text
    # no tpl.ge status screen for the manager
    assert not [c for c in sent_to(calls, OWNER, SendMessage) if c.text.startswith(("🛡", "⚠️"))]


def test_manager_card_shows_manual_upload_as_the_primary_action(h, settings, ids):
    _, order = _paid_review(h)
    calls = h.press(make_user(OWNER), f"mg:confirm:{order.id}")

    (card,) = [c for c in calls if isinstance(c, EditMessageText) and c.chat_id == OWNER]
    assert "✅ Оплата подтверждена — оформите полис" in card.text
    assert buttons(card)[0] == ("📄 Загрузить готовый полис", f"mg:policy:{order.id}")
    labels = [t for t, _ in buttons(card)]
    for hidden in ("🛡 Оформить в tpl.ge", "💳 Оплатить в tpl.ge", "🔄 Новая ссылка на оплату"):
        assert hidden not in labels
    # the same from "📋 Заказы"
    listed = last_screen(h.press(make_user(OWNER), f"st:co:{order.id}:0"))
    assert buttons(listed)[0] == ("📄 Загрузить готовый полис", f"mg:policy:{order.id}")
    assert "🛡 Оформить в tpl.ge" not in [t for t, _ in buttons(listed)]


def test_uploaded_pdf_goes_to_the_orders_customer_and_can_be_sent_again(h, settings, ids, tpl):
    customer, order = _paid_review(h)
    h.press(make_user(OWNER), f"mg:confirm:{order.id}")

    h.press(make_user(OWNER), f"mg:policy:{order.id}")
    delivered = h.send_document(make_user(OWNER), b"%PDF-1.4", file_id="pol-400", mime_type="application/pdf", file_name="p.pdf")

    assert [d.document for d in sent_to(delivered, CUSTOMER_ID, SendDocument)] == ["pol-400"]
    assert sent_to(delivered, OWNER, SendDocument) == []
    assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value
    assert ("paid", "processing") in [(a, b) for a, b, _ in _history(settings, order.id)]
    # "📄 Прислать полис ещё раз" (customer) still works
    resent = sent_to(h.press(customer, f"o:resend_policy:{order.id}"), CUSTOMER_ID, SendDocument)
    assert [d.document for d in resent] == ["pol-400"]
    assert tpl.applications == []


def test_duplicate_confirmation_is_idempotent(h, settings, ids, tpl):
    _, order = _paid_review(h)
    h.press(make_user(OWNER), f"mg:confirm:{order.id}")

    again = h.press(make_user(OWNER), f"mg:confirm:{order.id}")

    assert _answers(again) == [texts.MGR_ALREADY_DONE]
    assert sent_to(again, CUSTOMER_ID, SendMessage) == []
    assert [(a, b) for a, b, _ in _history(settings, order.id)].count(("payment_review", "paid")) == 1
    assert tpl.applications == []


def test_existing_paid_order_resumes_through_manual_upload_after_restart(settings, ids, tpl):
    """Like ORDER-1082: confirmed earlier, still PAID, no tpl.ge issuance row.
    After a restart the manager opens it, uploads the PDF -- no new receipt,
    no second confirmation -- and old tpl.ge buttons stay inert."""
    first = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    _, order = _paid_review(first)
    first.press(make_user(OWNER), f"mg:confirm:{order.id}")
    first.close()

    second = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    try:
        stale = second.press(make_user(OWNER), f"op:start:{order.id}")  # a "🛡 Оформить в tpl.ge" from an old card
        assert _answers(stale) == [texts.MGR_TPL_AUTO_DISABLED]
        assert buttons(last_screen(stale))[0] == ("📄 Загрузить готовый полис", f"mg:policy:{order.id}")
        for action in ("retry", "link", "paid", "status"):
            second.press(make_user(OWNER), f"op:{action}:{order.id}")
        assert tpl.applications == [] and _order(settings, order.id).status == OrderStatus.PAID.value

        card = last_screen(second.press(make_user(OWNER), f"st:co:{order.id}:0"))
        second.press(make_user(OWNER), dict(buttons(card))["📄 Загрузить готовый полис"])
        delivered = second.send_document(make_user(OWNER), b"%PDF-1.4", file_id="pol-old", mime_type="application/pdf", file_name="p.pdf")

        assert [d.document for d in sent_to(delivered, CUSTOMER_ID, SendDocument)] == ["pol-old"]
        assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value
        history = [(a, b) for a, b, _ in _history(settings, order.id)]
        assert history.count(("payment_review", "paid")) == 1  # confirmed once, never again
    finally:
        second.close()


def test_automatic_issuance_still_runs_when_explicitly_enabled(tmp_path, monkeypatch):
    enabled = _env(monkeypatch, tmp_path, auto=True)
    assert enabled.telegram_bot.tpl_auto_issuance is True
    seed_catalog(enabled.app.db_file)
    fake = FakeTpl().install(monkeypatch)  # the real service, fake tpl.ge transport
    harness = BotHarness(enabled, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    try:
        _, order = _paid_review(harness)
        calls = harness.press(make_user(OWNER), f"mg:confirm:{order.id}")
        assert len(fake.applications) == 1
        assert any(c.text.startswith("🛡 Заявка создана в tpl.ge") for c in sent_to(calls, OWNER, SendMessage))
        (card,) = [c for c in calls if isinstance(c, EditMessageText) and c.chat_id == OWNER]
        assert buttons(card)[0] == ("🛡 Оформить в tpl.ge", f"op:start:{order.id}")
    finally:
        harness.close()


def test_explicit_false_is_off(tmp_path, monkeypatch):
    assert _env(monkeypatch, tmp_path, auto=False).telegram_bot.tpl_auto_issuance is False
