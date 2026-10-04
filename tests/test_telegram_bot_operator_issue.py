"""Operator issuance: a manager issues a TPL policy for a CUSTOMER from the
bot, through the same wizard and the SAME insurer integration
(app.integrations.tpl_ge.service) the web admin uses.

Never a real insurer call: every tpl.ge / Bank of Georgia function is
replaced at the module-function boundary (the same boundary the web tests
fake), and records exactly what it was sent. Fake Bot API, tmp DB."""

import asyncio
import dataclasses
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from aiogram.types import BufferedInputFile, CallbackQuery, Chat, Message, Update
from PIL import Image

from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.integrations.tpl_ge import repository as tpl_repo
from app.integrations.tpl_ge import service as tpl_service
from app.ocr.models import OcrResult
from app.ocr.provider import OcrProvider
from app.orders import files as order_files
from app.orders.repository import get_order_by_id, list_telegram_orders
from app.orders.state_machine import OrderStatus
from app.settings import load_settings
from app.telegram_bot import texts
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, nav, screens, seed_catalog, sent_to

VIN = "WVWZZZ1JZXW000001"
OWNER = 999
PROFILE = dataclasses.replace(
    TEST_PROFILE, username="OsagoTestBot", customer_email="tplgee@mail.ru", customer_phone="+995 574 22 06 25"
)
BOG_URL = "https://mpi.gc.ge/page1?merch_id=abc&o.id=ORDER-OID-1"
CUSTOMER = OcrResult(
    provider="fake", registration_number="AB123CD", vin=VIN, chassis_number=None, manufacturer="Toyota", model="Camry",
    policyholder_full_name="PETROV PETR", passport_number="751234567", citizenship="Russian Federation",
)
ISSUED = {"policyNumber": "TPL7635945", "policyId": 1234567, "documents": [{"file": "policy-TPL7635945.pdf", "url": "https://ext-stream.tpl.ge/p/policy.pdf"}]}
DOCUMENTS = [{"documentType": "Policy", "file": "policy-TPL7635945.pdf", "url": "https://ext-stream.tpl.ge/p/policy.pdf"}]


class Provider(OcrProvider):
    def __init__(self, result=CUSTOMER):
        self.result = result

    def recognize(self, images):
        return self.result


class FakeTpl:
    """Every outbound tpl.ge/BOG call, recorded. Knobs: set an attribute to
    an exception to make that call fail."""

    def __init__(self):
        self.applications: list[dict] = []
        self.bog_calls = 0
        self.policy_calls = 0
        self.downloads = 0
        self.create_error = None
        self.bog_error = None
        self.categories_error = None
        self.countries = [{"id": 52, "name": "Russia"}, {"id": 1, "name": "Georgia"}]
        self.policy = ISSUED

    def install(self, monkeypatch):
        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def categories(client):
            if self.categories_error:
                raise self.categories_error
            return [{"id": 7, "products": [{"productId": 11, "period": 30, "periodType": "D", "price": 30.0,
                                            "minDate": "2020-01-01T00:00:00", "maxDate": "2035-01-01T00:00:00"}]}]

        def create(client, payload):
            if self.create_error:
                raise self.create_error
            self.applications.append(payload)

        def bog(client, params):
            self.bog_calls += 1
            if self.bog_error:
                raise self.bog_error
            return BOG_URL

        def fetch_policy(client, o_id):
            self.policy_calls += 1
            return self.policy

        def download(client, url):
            self.downloads += 1
            return b"%PDF-1.4 the real policy"

        monkeypatch.setattr(tpl_service.tpl_client, "new_client", lambda: _Client())
        monkeypatch.setattr(tpl_service.catalog_client, "fetch_categories", categories)
        monkeypatch.setattr(tpl_service.catalog_client, "fetch_countries", lambda client: self.countries)
        monkeypatch.setattr(tpl_service.tpl_client, "create_application", create)
        monkeypatch.setattr(tpl_service.tpl_client, "initiate_bog_payment", bog)
        monkeypatch.setattr(tpl_service.tpl_client, "fetch_policy", fetch_policy)
        monkeypatch.setattr(tpl_service.tpl_client, "fetch_policy_documents", lambda client, o_id: DOCUMENTS)
        monkeypatch.setattr(tpl_service.tpl_client, "download_document", download)
        monkeypatch.setattr(tpl_service.time, "sleep", lambda seconds: None)
        return self


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+79495205223")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Владимир М.")
    monkeypatch.setenv("TPL_GE_STATIC_VISITOR_ID", "test-visitor-id")
    return load_settings(PROJECT_ROOT)


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


# ------------------------------------------------------------------ helpers


def _conn(settings):
    return get_connection(settings.app.db_file)


def _orders(settings, user_id):
    conn = _conn(settings)
    try:
        return list_telegram_orders(conn, bot_key=PROFILE.bot_key, telegram_user_id=user_id)
    finally:
        conn.close()


def _order(settings, order_id):
    conn = _conn(settings)
    try:
        return get_order_by_id(conn, order_id)
    finally:
        conn.close()


def _issuance(settings, order_id):
    conn = _conn(settings)
    try:
        return tpl_repo.get_issuance_by_order_id(conn, order_id)
    finally:
        conn.close()


def _history(settings, order_id):
    conn = _conn(settings)
    try:
        rows = conn.execute(
            "SELECT from_status, to_status, note FROM insurance_order_status_history WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
    finally:
        conn.close()
    return [(r["from_status"], r["to_status"], r["note"]) for r in rows]


def _photo() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 700), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _review(h, user):
    h.send_text(user, "/start")
    h.press(user, "p:passenger_car:30d")
    h.press(user, "d:tomorrow")
    h.press(user, "e:documents")
    return last_screen(h.send_album(user, f"g{user.id}-{datetime.now().timestamp()}", [("photo", f"f{user.id}-{i}-{datetime.now().timestamp()}", _photo()) for i in range(3)]))


def _button(screen, text) -> str:
    (data,) = [d for t, d in buttons(screen) if t == text]
    return data


def _confirmation(h, user):
    review = _review(h, user)
    return last_screen(h.press(user, _button(review, "🛡 Оформить страховку")))


def _issue(h, user):
    """review -> 🛡 -> ✅ Оформить полис. Returns (go callback data, calls)."""
    confirm = _confirmation(h, user)
    go = _button(confirm, "✅ Оформить полис")
    return go, h.press(user, go)


def _manager(h, user_id, username=None):
    screen = last_screen(h.press(make_user(OWNER), "st:invite:0:0"))
    token = screen.text.split("start=", 1)[1].split()[0]
    user = make_user(user_id, username)
    h.send_text(user, f"/start {token}")
    return user


def _texts(calls):
    return [c.text for c in screens(calls)]


# ------------------------------------------------------------ CUSTOMER


def test_customer_review_has_no_direct_issuance(h, settings, ids, tpl):
    customer = make_user(10)
    review = _review(h, customer)
    assert [t for t, _ in buttons(review)][0] == "✅ Всё верно"
    assert not any(d.startswith(("is:", "op:")) for _, d in buttons(review))
    assert "🛡 Оформить страховку" not in [t for t, _ in buttons(review)]


def test_customer_crafted_issuance_callbacks_are_refused(h, settings, ids, tpl):
    customer = make_user(11)
    _review(h, customer)
    for data in ("is:ask::", "is:go:abc:def", "op:status:1", "op:retry:1", "op:paid:1", "op:link:1", "op:resend:1"):
        calls = h.press(customer, data)
        alerts = [c for c in calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]
        assert alerts and alerts[0].text == texts.MGR_NO_ACCESS, data
        assert not screens(calls), data
    assert _orders(settings, 11) == [] and tpl.applications == []
    # their normal route is unchanged: Всё верно -> payment
    payment = last_screen(h.press(customer, "fc:confirm"))
    assert payment.text.startswith("💳 Оплата заказа") and "Владимир М." in payment.text


# ------------------------------------------------------------- MANAGER


def test_manager_review_offers_direct_issuance_and_the_payment_route(h, settings, ids, tpl):
    review = _review(h, make_user(OWNER))
    labels = [t for t, _ in buttons(review)]
    assert labels[:2] == ["🛡 Оформить страховку", "💳 Создать заказ с оплатой"]
    assert "✅ Всё верно" not in labels
    assert "✏️ Паспорт" in labels  # the ordinary edit actions stay


def test_manager_can_choose_the_normal_payment_order(h, settings, ids, tpl):
    manager = make_user(OWNER)
    review = _review(h, manager)
    payment = last_screen(h.press(manager, _button(review, "💳 Создать заказ с оплатой")))
    assert payment.text.startswith("💳 Оплата заказа") and "Сбербанк" in payment.text
    (order,) = _orders(settings, OWNER)
    assert order.payment_mode is None and order.status == OrderStatus.AWAITING_PAYMENT.value
    assert tpl.applications == []


def test_explicit_confirmation_before_any_insurer_call(h, settings, ids, tpl):
    confirm = _confirmation(h, make_user(OWNER))
    assert confirm.text.startswith("🛡 Оформить полис?")
    for line in ("Страхователь: PETROV PETR", "Паспорт: 751234567", "Гражданство: Россия", "Автомобиль: TOYOTA CAMRY",
                 "Госномер: AB123CD", f"VIN: {VIN}", "Период: 30 дней", "Начало:", "Стоимость: 2 149 ₽"):
        assert line in confirm.text, line
    assert [t for t, _ in buttons(confirm)] == ["✅ Оформить полис", "⬅️ Назад"]
    assert _button(confirm, "⬅️ Назад") == nav("checkout_review")
    assert tpl.applications == [] and tpl.bog_calls == 0 and _orders(settings, OWNER) == []


def test_direct_issuance_creates_an_auditable_operator_order_without_customer_payment(h, settings, ids, tpl):
    manager = make_user(OWNER, "boss")
    _, calls = _issue(h, manager)
    (order,) = _orders(settings, OWNER)
    # the order: operator origin, manager = actor/recipient, policyholder = the documents' person
    assert order.payment_mode == "operator" and order.is_operator_order
    assert order.created_by_telegram_user_id == OWNER and order.telegram_chat_id == OWNER
    assert (order.full_name, order.identification_number, order.citizenship) == ("PETROV PETR", "751234567", "Russia")
    assert order.contact_telegram is None  # never the manager's @username as the policyholder's contact
    assert (order.contact_email, order.contact_phone) == ("tplgee@mail.ru", "+995 574 22 06 25")
    assert order.status == OrderStatus.PROCESSING.value
    history = _history(settings, order.id)
    assert ("data_completed", "paid") in [(a, b) for a, b, _ in history]
    (bypass_note,) = [n for a, b, n in history if (a, b) == ("data_completed", "paid")]
    assert "payment collection bypassed" in bypass_note and str(OWNER) in bypass_note
    assert not any(b in ("awaiting_payment", "payment_review") for _, b, _ in history)  # no Sber, no receipt check
    # no Sber screen for anyone, nothing sent to a "customer" chat
    assert not any("Сбербанк" in t or "Оплата заказа" in t for t in _texts(calls))
    assert {getattr(c, "chat_id", OWNER) for c in calls if isinstance(c, SendMessage)} <= {OWNER}
    # the status screen: the TPL payment link for the company card
    status = last_screen(calls)
    assert status.text.startswith("🛡 Заявка создана в tpl.ge") and "Стоимость в tpl.ge: 30.00 GEL" in status.text
    assert (status.reply_markup.inline_keyboard[0][0].url, status.reply_markup.inline_keyboard[0][0].text) == (BOG_URL, "💳 Оплатить в tpl.ge")
    assert ("✅ Оплата TPL завершена", f"op:paid:{order.id}") in buttons(status)


def test_insurer_receives_exactly_the_customers_data(h, settings, ids, tpl):
    _issue(h, make_user(OWNER, "boss"))
    (payload,) = tpl.applications
    expected = {
        "vinCode": VIN, "vehicleRegistrationNumber": "AB123CD", "vehicleCategoryId": 7,
        "vehicleManufacturerName": "TOYOTA", "vehicleModelName": "CAMRY", "productId": 11, "insurerType": "I",
        "insurerTitle": "PETROV PETR", "insurerIdentificationNumber": "751234567", "insurerEmail": "tplgee@mail.ru",
        "insurerPhone": "+995 574 22 06 25", "insurerCitizenshipId": 52, "vehicleOwnerTitle": "PETROV PETR",
        "vehicleDriverTitle": "PETROV PETR", "visitorId": "test-visitor-id",
    }
    for key, value in expected.items():
        assert payload[key] == value, key
    conn = _conn(settings)
    try:
        from app.catalog import repository as catalog_repo

        assert payload["vehicleManufacturerId"] == catalog_repo.get_manufacturer(conn, ids["TOYOTA"]).external_id
        assert payload["vehicleModelId"] == catalog_repo.get_model(conn, ids["TOYOTA/CAMRY"]).external_id
    finally:
        conn.close()
    order = _orders(settings, OWNER)[0]
    assert payload["startDate"] == order.start_date.isoformat()
    assert "boss" not in str(payload) and "Test" not in str(payload)  # nothing of the manager


def test_other_fallback_mapping_is_the_same_as_the_web(settings, ids, tpl):
    haval = dataclasses.replace(CUSTOMER, manufacturer="Haval", model="H9 Premium")
    h = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider(haval))
    try:
        _issue(h, make_user(OWNER))
        (payload,) = tpl.applications
        assert (payload["vehicleManufacturerId"], payload["vehicleManufacturerName"]) == (1, "Other")
        assert (payload["vehicleModelId"], payload["vehicleModelName"]) == (-1, "Other")
        order = _orders(settings, OWNER)[0]
        assert (order.vehicle_make_document, order.vehicle_model_document) == ("Haval", "H9 Premium")
    finally:
        h.close()


# ---------------------------------------------------------- IDEMPOTENCY


def test_a_repeated_issue_press_never_creates_a_second_policy(h, settings, ids, tpl):
    manager = make_user(OWNER)
    go, first = _issue(h, manager)
    bog_after_first = tpl.bog_calls
    again = h.press(manager, go)  # double tap / replayed callback
    assert len(tpl.applications) == 1 and len(_orders(settings, OWNER)) == 1
    assert tpl.bog_calls == bog_after_first  # not even a new payment link (could orphan a payment in progress)
    assert last_screen(again).text.startswith("🛡 Заявка создана в tpl.ge")


def test_simultaneous_issue_presses_create_one_order_and_one_application(h, settings, ids, tpl):
    manager = make_user(OWNER)
    confirm = _confirmation(h, manager)
    go = _button(confirm, "✅ Оформить полис")

    def update(n):
        message = Message(message_id=70_000, date=datetime.now(timezone.utc), chat=Chat(id=OWNER, type="private"), text="…")
        return Update(update_id=80_000 + n, callback_query=CallbackQuery(id=f"dup-{n}", from_user=manager, chat_instance="ci", data=go, message=message))

    async def both():
        await asyncio.gather(*(h.dispatcher.feed_update(h.bot, update(n)) for n in (1, 2)))

    h.loop.run_until_complete(both())
    assert len(_orders(settings, OWNER)) == 1 and len(tpl.applications) == 1


def test_restart_after_issuance_never_issues_again(settings, ids, tpl):
    manager = make_user(OWNER)
    first = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    go, _ = _issue(first, manager)
    first.close()
    second = BotHarness(settings, profile=PROFILE, manager_ids={OWNER}, ocr_provider=Provider())
    try:
        # the old button after a restart (Telegram message ids only grow; the new harness counts from 1)
        replay = second.press(manager, go, message_id=10**6)
        assert len(tpl.applications) == 1 and len(_orders(settings, OWNER)) == 1
        assert last_screen(replay).text.startswith("🛡 Заявка создана в tpl.ge")
        order = _orders(settings, OWNER)[0]
        done = second.press(manager, f"op:paid:{order.id}")
        assert [type(c.document) for c in sent_to(done, OWNER, SendDocument)] == [BufferedInputFile]
    finally:
        second.close()


def test_unknown_insurer_outcome_is_never_resent_only_probed(h, settings, ids, tpl):
    manager = make_user(OWNER)
    tpl.create_error = httpx.ReadTimeout("no answer")
    go, calls = _issue(h, manager)
    order = _orders(settings, OWNER)[0]
    assert _issuance(settings, order.id).is_application_requested
    assert last_screen(calls).text.startswith("⏳ Заявка отправлена в tpl.ge, но ответ не получен")
    tpl.create_error = None
    # right away: another attempt is (maybe) still in flight -> nothing is sent
    h.press(manager, f"op:retry:{order.id}")
    h.press(manager, go)
    assert tpl.applications == [] and tpl.bog_calls == 0
    # later: probe TPL with the SAME uid -- it knows the application -> link, no second POST
    conn = _conn(settings)
    try:
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        conn.execute("UPDATE insurance_tpl_issuance SET application_requested_at = ? WHERE order_id = ?", (old, order.id))
        conn.commit()
    finally:
        conn.close()
    tpl.bog_error = tpl_service.tpl_client.BogHandoffHttpError("unknown uid")
    unknown = h.press(manager, f"op:retry:{order.id}")
    assert "ответ не получен" in last_screen(unknown).text and tpl.applications == []
    tpl.bog_error = None
    recovered = h.press(manager, f"op:retry:{order.id}")
    assert last_screen(recovered).text.startswith("🛡 Заявка создана в tpl.ge")
    assert tpl.applications == []  # the application was never sent a second time
    assert len(_orders(settings, OWNER)) == 1


# ------------------------------------------------------------- FAILURES


def test_validation_failure_keeps_the_order_and_allows_a_corrected_attempt(h, settings, ids, tpl):
    manager = make_user(OWNER)
    tpl.countries = [{"id": 1, "name": "Georgia"}]  # TPL doesn't know the citizenship
    _, calls = _issue(h, manager)
    failed = last_screen(calls)
    assert failed.text.startswith("❌ Полис не оформлен — данные не подходят для tpl.ge")
    assert "В tpl.ge ничего не создано" in failed.text
    assert ("✏️ Исправить данные", nav("checkout_review")) in buttons(failed)
    (first,) = _orders(settings, OWNER)
    assert first.status == OrderStatus.PAID.value and _issuance(settings, first.id).is_failed  # kept, auditable
    assert tpl.applications == []
    # the checkout is NOT lost: back to the review, data intact
    review = last_screen(h.press(manager, nav("checkout_review")))
    assert "PETROV PETR" in review.text
    tpl.countries = [{"id": 52, "name": "Russia"}]
    confirm = last_screen(h.press(manager, _button(review, "🛡 Оформить страховку")))
    h.press(manager, _button(confirm, "✅ Оформить полис"))
    orders = {o.id: o for o in _orders(settings, OWNER)}
    assert orders[first.id].status == OrderStatus.CANCELLED.value  # superseded, nothing existed at TPL for it
    assert len(tpl.applications) == 1 and len(orders) == 2


def test_insurer_rejection_is_shown_and_can_be_retried_safely(h, settings, ids, tpl):
    manager = make_user(OWNER)
    tpl.create_error = tpl_service.tpl_client.TplPoliciesError("POST /api/policies -> 400")
    _, calls = _issue(h, manager)
    assert last_screen(calls).text.startswith("❌ tpl.ge отклонил заявку")
    order = _orders(settings, OWNER)[0]
    assert _issuance(settings, order.id).is_failed
    tpl.create_error = None
    retried = h.press(manager, f"op:retry:{order.id}")
    assert last_screen(retried).text.startswith("🛡 Заявка создана в tpl.ge")
    assert len(tpl.applications) == 1 and len(_orders(settings, OWNER)) == 1


def test_temporary_insurer_errors_are_retryable_without_duplicates(h, settings, ids, tpl):
    manager = make_user(OWNER)
    tpl.categories_error = httpx.ConnectError("tpl.ge down")
    _, calls = _issue(h, manager)
    assert last_screen(calls).text.startswith("⚠️ Временная ошибка связи с tpl.ge")
    order = _orders(settings, OWNER)[0]
    tpl.categories_error = None
    tpl.bog_error = tpl_service.tpl_client.BogHandoffHttpError("BOG down")
    after_create = h.press(manager, f"op:retry:{order.id}")
    assert len(tpl.applications) == 1  # created; only the payment link failed
    assert ("🔄 Новая ссылка на оплату", f"op:link:{order.id}") in buttons(last_screen(after_create))
    tpl.bog_error = None
    linked = h.press(manager, f"op:link:{order.id}")
    assert last_screen(linked).text.startswith("🛡 Заявка создана в tpl.ge")
    h.press(manager, f"op:retry:{order.id}")
    assert len(tpl.applications) == 1


def test_changed_data_after_the_confirmation_needs_a_new_confirmation(h, settings, ids, tpl):
    manager = make_user(OWNER)
    confirm = _confirmation(h, manager)
    go = _button(confirm, "✅ Оформить полис")
    h.press(manager, nav("passport", "checkout_review"))
    h.send_text(manager, "C01234567")
    again = last_screen(h.press(manager, go))  # the old confirmation's button
    assert again.text.startswith(texts.OP_CHANGED) and "Паспорт: C01234567" in again.text
    assert tpl.applications == [] and _orders(settings, OWNER) == []
    h.press(manager, _button(again, "✅ Оформить полис"))
    assert tpl.applications[0]["insurerIdentificationNumber"] == "C01234567"


# --------------------------------------------------------- SUCCESS / PDF


def test_policy_pdf_goes_to_the_issuing_manager_once(h, settings, ids, tpl):
    manager = make_user(OWNER)
    _issue(h, manager)
    order = _orders(settings, OWNER)[0]
    calls = h.press(manager, f"op:paid:{order.id}")
    (pdf,) = sent_to(calls, OWNER, SendDocument)
    assert isinstance(pdf.document, BufferedInputFile) and pdf.document.data == b"%PDF-1.4 the real policy"
    assert pdf.caption == f"🛡 Полис TPL7635945 · заказ {order.public_number}\nСтрахователь: PETROV PETR"
    done = last_screen(calls)
    assert done.text.startswith("🎉 Полис TPL7635945 оформлен.")
    issuance = _issuance(settings, order.id)
    assert issuance.policy_number == "TPL7635945" and issuance.is_policy_retrieved and issuance.is_sent_to_operator
    assert _order(settings, order.id).status == OrderStatus.POLICY_READY.value
    conn = _conn(settings)
    try:
        (policy_file,) = order_files.list_files(conn, order.id, order_files.KIND_POLICY)
    finally:
        conn.close()
    assert policy_file.telegram_file_id.startswith("uploaded-")
    # pressed again: never a second download/send
    again = h.press(manager, f"op:paid:{order.id}")
    assert sent_to(again, OWNER, SendDocument) == [] and tpl.downloads == 1
    resent = h.press(manager, f"op:resend:{order.id}")
    assert [d.document for d in sent_to(resent, OWNER, SendDocument)] == [policy_file.telegram_file_id]


def test_policy_not_ready_yet_is_a_retry_later(h, settings, ids, tpl):
    manager = make_user(OWNER)
    _issue(h, manager)
    order = _orders(settings, OWNER)[0]
    tpl.policy = {"policyNumber": None, "documents": []}
    calls = h.press(manager, f"op:paid:{order.id}")
    assert last_screen(calls).text.startswith("⏳ Оплата отмечена") and sent_to(calls, OWNER, SendDocument) == []
    tpl.policy = ISSUED
    assert sent_to(h.press(manager, f"op:paid:{order.id}"), OWNER, SendDocument)
    assert len(tpl.applications) == 1


def test_operator_order_is_listed_and_opens_its_issuance_screen(h, settings, ids, tpl):
    manager = make_user(OWNER, "boss")
    _issue(h, manager)
    order = _orders(settings, OWNER)[0]
    listing = last_screen(h.press(manager, "st:orders:0:0"))
    assert (f"{order.public_number} · 🛡 Оформление", f"st:co:{order.id}:0") in buttons(listing)
    card = last_screen(h.press(manager, f"st:co:{order.id}:0"))
    assert "🛡 Оформлен менеджером @boss: оплата клиентом через бота не собиралась" in card.text
    assert "ФИО PETROV PETR" in card.text
    assert not any(d.startswith("mg:") for _, d in buttons(card))  # no customer payment actions
    status = last_screen(h.press(manager, _button(card, "🛡 Оформление полиса")))
    assert status.text.startswith("🛡 Заявка создана в tpl.ge")
    # the customer payment handlers refuse an operator order outright
    assert [c.text for c in h.press(manager, f"mg:confirm:{order.id}") if isinstance(c, AnswerCallbackQuery)] == [texts.MGR_ORDER_NOT_FOUND]


def test_operator_order_is_never_offered_as_the_managers_own_order(h, settings, ids, tpl):
    manager = make_user(OWNER)
    _issue(h, manager)
    start = last_screen(h.send_text(manager, "/start"))
    assert "незавершённый заказ" not in start.text
    assert not any(d.startswith("o:") for _, d in buttons(start))
    # a fresh checkout starts clean -- nothing of the issued customer is left in the draft
    review = _review(h, manager)
    assert review.text.count("PETROV PETR") == 1  # recognized again from the new photos only


# ---------------------------------------------------------- AUTHORIZATION


def test_a_removed_manager_loses_direct_issuance_immediately(h, settings, ids, tpl):
    manager = _manager(h, 50, "temp")
    confirm = _confirmation(h, manager)
    go = _button(confirm, "✅ Оформить полис")
    h.press(make_user(OWNER), "st:rm:50:0")
    calls = h.press(manager, go)
    assert [c.text for c in calls if isinstance(c, AnswerCallbackQuery)] == [texts.MGR_NO_ACCESS]
    assert tpl.applications == [] and _orders(settings, 50) == []
    # their review is a customer's again
    assert [t for t, _ in buttons(last_screen(h.press(manager, nav("checkout_review"))))][0] == "✅ Всё верно"


def test_a_manager_can_run_the_whole_issuance_and_the_owner_can_follow_up(h, settings, ids, tpl):
    manager = _manager(h, 51, "anna")
    _issue(h, manager)
    order = _orders(settings, 51)[0]
    assert order.created_by_telegram_user_id == 51
    calls = h.press(make_user(OWNER), f"op:paid:{order.id}")  # another staff member finishes it
    assert [type(c.document) for c in sent_to(calls, 51, SendDocument)] == [BufferedInputFile]  # PDF to the initiator
    assert sent_to(calls, OWNER, SendDocument) == []


# ------------------------------------------------------ CUSTOMER UNCHANGED


def test_normal_customer_payment_flow_and_manual_fallback(h, settings, ids, tpl):
    settings.telegram_bot.tpl_auto_issuance = True  # this test: the AUTOMATIC issuance (off by default)
    customer = make_user(60)
    _review(h, customer)
    h.press(customer, "fc:confirm")
    (order,) = _orders(settings, 60)
    assert order.payment_mode is None and order.status == OrderStatus.AWAITING_PAYMENT.value
    assert tpl.applications == []  # nothing goes to the insurer before OUR payment is confirmed
    h.send_photo(customer, b"receipt", file_id="rcpt-60")
    assert _order(settings, order.id).status == OrderStatus.PAYMENT_REVIEW.value
    assert tpl.applications == []
    h.press(make_user(OWNER), f"mg:confirm:{order.id}")  # -> the shared tpl.ge issuance starts
    assert len(tpl.applications) == 1 and _order(settings, order.id).status == OrderStatus.PROCESSING.value
    # the manual PDF upload is still there as an explicit fallback
    h.press(make_user(OWNER), f"mg:policy:{order.id}")
    delivered = h.send_document(make_user(OWNER), b"%PDF-1.4", file_id="pol-60", mime_type="application/pdf", file_name="p.pdf")
    assert [d.document for d in sent_to(delivered, 60, SendDocument)] == ["pol-60"]
    assert len(tpl.applications) == 1
