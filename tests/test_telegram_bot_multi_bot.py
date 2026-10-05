"""Multi-bot support: a SECOND Telegram bot profile for Turkey
(bot_key="osago24rt", country_code="TR", the real @OSAGOTRbot) reusing the
EXACT SAME handlers/DB/architecture as @OSAGO24GEbot -- never a separate
implementation (see config/config.yaml's telegram_bots.osago24rt and
app.telegram_bot.categories).

These tests prove three things:
1. Georgia's own behaviour (categories, 💰 Цены, menu) is completely
   unchanged by this work.
2. The two bots are fully isolated from each other despite sharing one
   SQLite file and one running codebase: staff, sessions, orders, price
   overrides and the outbox never cross bot_key/country_code boundaries.
3. The Turkey-specific product (motorcycle/passenger_car + "bus" relabeled
   "Truck / Camper") only ever appears for the Turkey-profiled bot, and the
   website's own TR category list (catalog.enabled_category_codes_by_country)
   is untouched by it.

All offline: fake Bot API session (telegram_bot_helpers.RecordingSession),
tmp DB, real config.yaml (NOT the web-test fixture config) so the real
osago24ge/osago24rt profiles and real GE/TR pricing are exercised.
"""

import dataclasses

import pytest
from aiogram.methods import AnswerCallbackQuery, SendDocument

from app.checkout.rules import (
    allowed_category_codes,
    auto_assigns_start_date,
    requires_date_of_birth,
    requires_model_year,
)
from app.countries import COUNTRIES
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.orders.repository import get_order_by_id, list_telegram_orders
from app.pricing import overrides as price_overrides
from app.pricing.provider import available_periods, get_period
from app.sessions.repository import get_draft
from app.settings import load_settings
from app.telegram_bot import order_views, staff, texts
from app.telegram_bot.config import load_bot_config
from app.telegram_bot.keyboards import PricesCb, StaffCb
from app.telegram_bot.sessions import session_id_for
from app.validation import validate_date_of_birth, validate_model_year
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, seed_catalog, sent_to

OWNER_GE = 999
OWNER_TR = 888
VIN = "WVWZZZ1JZXW000001"

# username must be set on both (not None) -- staff_panel._invite() falls
# back to a real bot.me() Bot API call otherwise, which RecordingSession
# doesn't implement (same requirement as test_telegram_bot_staff.py's own
# PROFILE constant).
GE_PROFILE = dataclasses.replace(TEST_PROFILE, username="OsagoGeTestBot")
# Matches the real production config.yaml telegram_bots.osago24rt block --
# category order and fixed contacts both per task "FIX DEFAULT CUSTOMER
# CONTACTS FOR TR"/"CHANGE TR CATEGORY ORDER".
TR_EMAIL, TR_PHONE = "tplgee@mail.ru", "+995574220625"
TR_PROFILE = dataclasses.replace(
    TEST_PROFILE,
    bot_key="osago24rt",
    country_code="TR",
    username="OsagoTrTestBot",
    intro_title="🇹🇷 ОСАГО Турции",
    intro_text="Оформите страховку автомобиля для поездки в Турцию онлайн.",
    customer_email=TR_EMAIL,
    customer_phone=TR_PHONE,
    category_codes=("passenger_car", "motorcycle", "bus"),
    category_labels={"bus": "🚛 Truck / Camper"},
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)  # real config.yaml, real osago24rt profile
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+79495205223")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Владимир М.")
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


@pytest.fixture
def ge(settings, ids):
    h = BotHarness(settings, profile=GE_PROFILE, manager_ids={OWNER_GE})
    yield h
    h.close()


@pytest.fixture
def tr(settings, ids):
    h = BotHarness(settings, profile=TR_PROFILE, manager_ids={OWNER_TR})
    yield h
    h.close()


def _conn(settings):
    return get_connection(settings.app.db_file)


def _overrides(settings, country_code):
    conn = _conn(settings)
    try:
        return {(o.vehicle_category_code, o.period_code): o for o in price_overrides.list_overrides(conn, country_code)}
    finally:
        conn.close()


def _set_override(settings, *, country_code, category, period, price, by="t"):
    conn = _conn(settings)
    try:
        price_overrides.upsert_override(
            conn, country_code=country_code, vehicle_category_code=category, period_code=period, price_rub=price, updated_by=by
        )
        conn.commit()
    finally:
        conn.close()


def _name(user_id: int) -> str:
    return "Client " + "".join(chr(ord("A") + int(d)) for d in str(user_id))


def _alerts(calls):
    return [c for c in calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]


def _place_order(
    h, user, ids, *, category="passenger_car", period="30d", plate="AB123CD", model_year="2015", date_of_birth="15.05.1990"
):
    """A real order through the actual checkout conversation (manual entry,
    not OCR) -- works identically regardless of which bot profile h runs,
    since nothing in the flow itself is country-hardcoded.

    model_year/date_of_birth are only ever asked for TR (requires_model_year/
    requires_date_of_birth) -- GE's steps list is unaffected, byte for byte.
    The start-date step ("d:tomorrow") is skipped entirely for TR
    (auto_assigns_start_date -- see task "REMOVE START-DATE STEP COMPLETELY
    FOR TURKEY"); GE keeps picking it exactly as before. email/phone are
    only sent for a profile that does NOT set fixed customer_email/
    customer_phone (GE_PROFILE here; TR_PROFILE matches the real osago24rt
    config, which does set them -- see task "FIX DEFAULT CUSTOMER CONTACTS
    FOR TR")."""
    country_code = h.config.profile.country_code
    fixed = h.config.profile.fixed_contacts()
    steps = [
        lambda: h.send_text(user, "/start"),
        lambda: h.press(user, f"p:{category}:{period}"),
    ]
    if not auto_assigns_start_date(country_code):
        steps.append(lambda: h.press(user, "d:tomorrow"))
    steps += [
        lambda: h.press(user, "e:manual"),
        lambda: h.send_text(user, plate),
        lambda: h.send_text(user, VIN),
        lambda: h.press(user, f"mf:{ids['TOYOTA']}"),
        lambda: h.press(user, f"md:{ids['TOYOTA/CAMRY']}"),
    ]
    if requires_model_year(country_code):
        steps.append(lambda: h.send_text(user, model_year))
    steps += [
        lambda: h.press(user, "vc"),
        lambda: h.send_text(user, _name(user.id)),
        lambda: h.send_text(user, "AB1234567"),
        lambda: h.press(user, f"cz:{COUNTRIES.index('Russia')}"),
    ]
    if requires_date_of_birth(country_code):
        steps.append(lambda: h.send_text(user, date_of_birth))
    if "contact_email" not in fixed:
        steps.append(lambda: h.send_text(user, f"client{user.id}@example.com"))
    if "contact_phone" not in fixed:
        steps.append(lambda: h.send_text(user, "+79991234567"))
    for step in steps:
        step()
    h.press(user, "fc:confirm")
    conn = _conn(h.dispatcher["settings"])
    try:
        return list_telegram_orders(conn, bot_key=h.config.profile.bot_key, telegram_user_id=user.id)[0]
    finally:
        conn.close()


def _through_model_selection(h, user, ids, *, plate="AB123CD"):
    """Drives the conversation up to (and including) picking the catalog
    model -- the common prefix shared by every model_year test below,
    regardless of whether the bot profile then asks for model_year. The
    start-date step is skipped entirely for TR (auto_assigns_start_date)."""
    h.send_text(user, "/start")
    h.press(user, "p:passenger_car:30d")
    if not auto_assigns_start_date(h.config.profile.country_code):
        h.press(user, "d:tomorrow")
    h.press(user, "e:manual")
    h.send_text(user, plate)
    h.send_text(user, VIN)
    h.press(user, f"mf:{ids['TOYOTA']}")
    return h.press(user, f"md:{ids['TOYOTA/CAMRY']}")


def _add_manager(h, owner_id, new_user_id):
    import re

    screen = last_screen(h.press(make_user(owner_id), "st:invite:0:0"))
    (link,) = re.findall(r"https://t\.me/\S+\?start=mgr_[A-Za-z0-9_-]+", screen.text)
    token = link.split("start=", 1)[1]
    user = make_user(new_user_id)
    h.send_text(user, f"/start {token}")
    return user


# ========================================================== 1. BOT PROFILE


def test_osago24ge_profile_loads_unchanged(monkeypatch):
    # Env vars must be set BEFORE load_settings() reads them -- the shared
    # `settings` fixture (used by the bot-harness tests below) is built
    # without these, so this test builds its own.
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_KEY", "osago24ge")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("TELEGRAM_BOT_MANAGER_IDS", str(OWNER_GE))
    profile = load_bot_config(load_settings(PROJECT_ROOT)).profile
    assert (profile.bot_key, profile.country_code, profile.username) == ("osago24ge", "GE", "OSAGO24GEbot")
    assert profile.category_codes is None and profile.category_labels is None


def test_osago24rt_profile_loads(monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_KEY", "osago24rt")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("TELEGRAM_BOT_MANAGER_IDS", str(OWNER_TR))
    profile = load_bot_config(load_settings(PROJECT_ROOT)).profile
    assert (profile.bot_key, profile.country_code, profile.username) == ("osago24rt", "TR", "OSAGOTRbot")
    assert profile.category_codes == ("passenger_car", "motorcycle", "bus")
    assert profile.category_labels == {"bus": "🚛 Truck / Camper"}
    assert profile.customer_email == "tplgee@mail.ru"
    assert profile.customer_phone == "+995574220625"


# =========================================================== 2. CATEGORIES


def test_ge_categories_and_order_unchanged(ge):
    screen = last_screen(ge.press(make_user(10), "m:apply"))
    labels = [t for t, _ in buttons(screen)]
    # Exact order of texts.CATEGORY_LABELS (unchanged by this task).
    assert labels == [
        "🚗 Легковой автомобиль", "🏍 Мотоцикл", "🚛 Грузовой автомобиль", "🚌 Автобус", "🚚 Прицеп", "🚜 Спецтехника",
    ]


def test_tr_shows_exactly_three_categories_in_order(tr):
    screen = last_screen(tr.press(make_user(20), "m:apply"))
    assert buttons(screen) == [
        ("🚗 Легковой автомобиль", "c:passenger_car"),
        ("🏍 Мотоцикл", "c:motorcycle"),
        ("🚛 Truck / Camper", "c:bus"),
    ]


def test_tr_does_not_show_truck_trailer_special_vehicle(tr):
    screen = last_screen(tr.press(make_user(21), "m:apply"))
    labels = [t for t, _ in buttons(screen)]
    assert "🚛 Грузовой автомобиль" not in labels  # that label belongs to "truck", not shown for TR
    assert "🚚 Прицеп" not in labels
    assert "🚜 Спецтехника" not in labels
    assert "🚌 Автобус" not in labels  # "bus" is present, but under the TR label, never Georgia's


def test_bus_internal_code_reuse_does_not_change_ge_label(ge, tr):
    ge_screen = last_screen(ge.press(make_user(11), "m:apply"))
    assert ("🚌 Автобус", "c:bus") in buttons(ge_screen)
    tr_screen = last_screen(tr.press(make_user(22), "m:apply"))
    assert ("🚛 Truck / Camper", "c:bus") in buttons(tr_screen)


def test_website_tr_category_list_is_untouched_by_the_bot(settings):
    """catalog.enabled_category_codes_by_country.TR (the website's own list)
    must still be exactly what it was before this bot existed -- "bus" is
    NOT in it; the bot's own category_codes override is a completely
    separate mechanism (see app.telegram_bot.categories)."""
    assert allowed_category_codes(settings, "TR") == ["passenger_car", "motorcycle", "truck", "special_vehicle"]
    assert allowed_category_codes(settings, "GE") is None  # unrestricted, unchanged


# ======================================================= 3. CUSTOMER FLOW


def test_turkey_customer_can_complete_a_full_order(tr, ids):
    customer = make_user(30)
    order = _place_order(tr, customer, ids)
    assert order.country_code == "TR"
    assert order.bot_key == "osago24rt"
    assert order.vehicle_category_code == "passenger_car"
    assert order.period_code == "30d"
    assert order.price_customer_minor is not None and order.price_customer_minor > 0


def test_turkey_receipt_manager_confirm_manual_pdf_and_delivery(tr, ids):
    customer = make_user(31)
    order = _place_order(tr, customer, ids)
    tr.send_photo(customer, b"receipt", file_id="tr-receipt-1")

    # manager confirms payment
    calls = tr.press(make_user(OWNER_TR), f"mg:confirm:{order.id}")
    assert not _alerts(calls)
    conn = _conn(tr.dispatcher["settings"])
    try:
        assert get_order_by_id(conn, order.id).status == "paid"
    finally:
        conn.close()

    # manager uploads the manually-issued policy PDF
    tr.press(make_user(OWNER_TR), f"mg:policy:{order.id}")
    calls = tr.send_document(
        make_user(OWNER_TR), b"%PDF-1.4 fake", file_id="tr-policy-1", mime_type="application/pdf", file_name="policy.pdf"
    )
    # delivered straight to the customer's own chat
    assert sent_to(calls, customer.id, SendDocument)


# ======================================================== 4. BOT ISOLATION


def test_ge_order_cannot_be_operated_through_tr_bot_callbacks(ge, tr, ids):
    ge_order = _place_order(ge, make_user(40), ids)
    ge.send_photo(make_user(40), b"receipt", file_id="ge-r-1")

    calls = tr.press(make_user(OWNER_TR), f"mg:confirm:{ge_order.id}")
    (alert,) = _alerts(calls)
    assert alert.text == texts.MGR_ORDER_NOT_FOUND

    conn = _conn(tr.dispatcher["settings"])
    try:
        assert get_order_by_id(conn, ge_order.id).status != "paid"
    finally:
        conn.close()


def test_tr_order_cannot_be_operated_through_ge_bot_callbacks(ge, tr, ids):
    tr_order = _place_order(tr, make_user(41), ids)
    tr.send_photo(make_user(41), b"receipt", file_id="tr-r-2")

    calls = ge.press(make_user(OWNER_GE), f"mg:confirm:{tr_order.id}")
    (alert,) = _alerts(calls)
    assert alert.text == texts.MGR_ORDER_NOT_FOUND

    conn = _conn(ge.dispatcher["settings"])
    try:
        assert get_order_by_id(conn, tr_order.id).status != "paid"
    finally:
        conn.close()


def test_sessions_are_isolated_by_bot_key_for_the_same_numeric_user(ge, tr):
    same_user = make_user(50)
    ge.send_text(same_user, "/start")
    tr.send_text(same_user, "/start")

    conn = _conn(ge.dispatcher["settings"])
    try:
        ge_draft = get_draft(conn, session_id_for("testbot", 50))
        tr_draft = get_draft(conn, session_id_for("osago24rt", 50))
        assert ge_draft is not None and tr_draft is not None
        assert ge_draft.get("country_code") == "GE"
        assert tr_draft.get("country_code") == "TR"
    finally:
        conn.close()


def test_staff_is_isolated_by_bot_key(ge, tr):
    # OWNER_GE is a staff member of the GE bot only -- never implicitly TR staff.
    conn = _conn(ge.dispatcher["settings"])
    try:
        assert staff.role_of(conn, "testbot", OWNER_GE) == "owner"
        assert staff.role_of(conn, "osago24rt", OWNER_GE) is None
        assert staff.role_of(conn, "osago24rt", OWNER_TR) == "owner"
        assert staff.role_of(conn, "testbot", OWNER_TR) is None
    finally:
        conn.close()

    # And the TR bot's own menu reflects that: OWNER_GE gets the plain customer menu on TR.
    screen = last_screen(tr.send_text(make_user(OWNER_GE), "/start"))
    assert texts.BTN_STAFF_ORDERS not in [t for t, _ in buttons(screen)]


def test_outbox_jobs_use_the_correct_bot_key(ge, tr, ids):
    """A confirmed payment enqueues a card-update outbox job tagged with
    THIS bot's own bot_key (app.notifications.bot_outbox) -- never the
    other bot's."""
    order = _place_order(tr, make_user(42), ids)
    tr.send_photo(make_user(42), b"receipt", file_id="tr-r-3")
    tr.press(make_user(OWNER_TR), f"mg:confirm:{order.id}")

    conn = _conn(tr.dispatcher["settings"])
    try:
        rows = conn.execute(
            "SELECT bot_key FROM telegram_outbox WHERE order_id = ?", (order.id,)
        ).fetchall()
        assert rows, "expected at least one outbox job for the confirmed order"
        assert all(row["bot_key"] == "osago24rt" for row in rows)
    finally:
        conn.close()


# =============================================================== 5. PRICING


def test_tr_price_override_does_not_affect_ge(settings, ids):
    # `ids` (seed_catalog -> init_db) ensures insurance_price_overrides exists
    # -- these two tests write overrides directly, without going through a
    # BotHarness (which would call init_db() itself).
    _set_override(settings, country_code="TR", category="passenger_car", period="30d", price=1234)
    ge_price = get_period(settings, "GE", "passenger_car", "30d").price_rub
    assert ge_price == 2149  # real config default, untouched
    tr_price = get_period(settings, "TR", "passenger_car", "30d").price_rub
    assert tr_price == 1234


def test_ge_price_override_does_not_affect_tr(settings, ids):
    _set_override(settings, country_code="GE", category="passenger_car", period="30d", price=5555)
    tr_default = available_periods(load_settings(PROJECT_ROOT), "TR", "passenger_car")
    tr_30d = next(p for p in tr_default if p.code == "30d")
    assert tr_30d.price_rub != 5555


def test_prices_screen_shows_only_turkey_categories_and_title(tr):
    owner = make_user(OWNER_TR)
    screen = last_screen(tr.press(owner, StaffCb(a="menu").pack()))
    screen = last_screen(tr.press(owner, PricesCb(a="menu").pack()))
    assert "ОСАГО Турции" in screen.text
    assert "Грузия" not in screen.text
    category_buttons = [t for t, d in buttons(screen) if d.startswith("pr:cat:")]
    assert category_buttons == ["🚗 Легковой автомобиль", "🏍 Мотоцикл", "🚛 Truck / Camper"]


def test_prices_screen_ge_title_unchanged(ge):
    owner = make_user(OWNER_GE)
    screen = last_screen(ge.press(owner, PricesCb(a="menu").pack()))
    assert texts.PRICES_TITLE in screen.text


def test_tr_manager_can_set_a_price_for_the_unpriced_bus_category(tr):
    owner = make_user(OWNER_TR)
    tr.press(owner, PricesCb(a="menu").pack())
    tr.press(owner, PricesCb(a="cat", c="bus").pack())
    screen = last_screen(tr.press(owner, PricesCb(a="period", c="bus", p="30d").pack()))
    assert "не задана" in screen.text  # genuinely unpriced today -- never a guessed number

    confirm_screen = last_screen(tr.send_text(owner, "1999"))
    confirm_data = dict(buttons(confirm_screen))[texts.BTN_PRICES_CONFIRM]
    tr.press(owner, confirm_data)

    assert _overrides(tr.dispatcher["settings"], "TR")[("bus", "30d")].price_rub == 1999
    assert _overrides(tr.dispatcher["settings"], "GE") == {}


# =============================================================== 6. MANAGER


def test_turkey_manager_menu(tr):
    manager = _add_manager(tr, OWNER_TR, 60)
    labels = [t for t, _ in buttons(last_screen(tr.send_text(manager, "/start")))]
    assert labels == ["🚗 Оформить страховку", "📋 Заказы", "⏳ Ожидают оплаты", "💰 Цены"]
    owner_labels = [t for t, _ in buttons(last_screen(tr.send_text(make_user(OWNER_TR), "/start")))]
    assert owner_labels == [*labels, "👥 Менеджеры"]


def test_turkey_order_list_shows_only_turkey_orders(ge, tr, ids):
    tr_order = _place_order(tr, make_user(70), ids)
    _place_order(ge, make_user(71), ids)  # a GE order that must never appear on the TR list

    screen = last_screen(tr.press(make_user(OWNER_TR), "st:orders:0:0"))
    order_ids = {d.split(":")[2] for _, d in buttons(screen) if d.startswith("st:co:")}
    assert order_ids == {str(tr_order.id)}


def test_turkey_pending_payments_shows_only_turkey_orders(ge, tr, ids):
    tr_order = _place_order(tr, make_user(72), ids)
    tr.send_photo(make_user(72), b"receipt", file_id="tr-r-4")
    ge_order = _place_order(ge, make_user(73), ids)
    ge.send_photo(make_user(73), b"receipt", file_id="ge-r-4")

    screen = last_screen(tr.press(make_user(OWNER_TR), "st:pending:0:0"))
    pending_ids = {d.split(":")[2] for _, d in buttons(screen) if d.startswith("st:cp:")}
    assert pending_ids == {str(tr_order.id)}
    assert str(ge_order.id) not in pending_ids


def test_manager_invites_and_roles_persist_separately_per_bot(ge, tr):
    manager = _add_manager(tr, OWNER_TR, 80)
    conn = _conn(tr.dispatcher["settings"])
    try:
        assert staff.role_of(conn, "osago24rt", 80) == "manager"
        assert staff.role_of(conn, "testbot", 80) is None
    finally:
        conn.close()


# ================================================= 7. RESTART / IDEMPOTENCY


def test_turkey_bot_survives_a_restart_with_its_own_state(settings, ids):
    first = BotHarness(settings, profile=TR_PROFILE, manager_ids={OWNER_TR})
    order = _place_order(first, make_user(90), ids)
    first.close()

    second = BotHarness(settings, profile=TR_PROFILE, manager_ids={OWNER_TR})
    try:
        conn = _conn(settings)
        try:
            assert get_order_by_id(conn, order.id) is not None
            assert staff.role_of(conn, "osago24rt", OWNER_TR) == "owner"
        finally:
            conn.close()
        # the owner's menu still shows after restart, exactly like before
        labels = [t for t, _ in buttons(last_screen(second.send_text(make_user(OWNER_TR), "/start")))]
        assert "💰 Цены" in labels
    finally:
        second.close()


# ========================================= 8. PAYMENT (shared TR/GE config)


def test_tr_and_ge_customers_see_the_identical_payment_configuration(ge, tr, ids):
    """Business decision: TR intentionally reuses GE's own Sberbank/RUB
    TELEGRAM_PAYMENT_* configuration -- settings.telegram_payment is a single
    global value nothing bot-specific was ever wired into, so this is true by
    construction; this test is the regression guard against that changing."""
    ge_order = _place_order(ge, make_user(100), ids)
    tr_order = _place_order(tr, make_user(101), ids)
    ge_text, ge_shown = order_views.payment_details_text(ge.dispatcher["settings"], ge_order)
    tr_text, tr_shown = order_views.payment_details_text(tr.dispatcher["settings"], tr_order)
    assert ge_shown and tr_shown

    def normalize(text: str, order) -> str:
        return text.replace(order.public_number, "#").replace(order_views.amount_rub(order), "AMOUNT")

    assert normalize(ge_text, ge_order) == normalize(tr_text, tr_order)
    assert "₽" in tr_text  # TR customer pays in RUB, same as GE -- never TRY/TL


def test_tr_order_currency_is_rub_same_as_ge(ge, tr, ids):
    ge_order = _place_order(ge, make_user(102), ids)
    tr_order = _place_order(tr, make_user(103), ids)
    assert tr_order.customer_currency == ge_order.customer_currency == "RUB"


# ============================ 9. TR REQUIRED FIELDS (model_year / date_of_birth)


def test_tr_vehicle_flow_asks_for_model_year_ge_does_not(ge, tr, ids):
    ge_screen = last_screen(_through_model_selection(ge, make_user(110), ids))
    assert any(d == "vc" for _, d in buttons(ge_screen))  # GE goes straight to vehicle_review

    tr_screen = last_screen(_through_model_selection(tr, make_user(111), ids))
    assert texts.ASK_MODEL_YEAR.splitlines()[0] in tr_screen.text
    assert not any(d == "vc" for _, d in buttons(tr_screen))  # not on vehicle_review yet


def test_ge_vehicle_flow_lands_directly_on_vehicle_review(ge, ids):
    screen = last_screen(_through_model_selection(ge, make_user(112), ids))
    assert any(d == "vc" for _, d in buttons(screen))
    assert "Год выпуска" not in screen.text


def test_tr_model_year_validation_matches_web_checkout(tr, ids):
    current_year = today_in_georgia().year
    _, too_old_error = validate_model_year(str(1899), current_year=current_year)
    assert too_old_error  # sanity: this really is invalid per the shared validator

    user = make_user(113)
    _through_model_selection(tr, user, ids)
    rejected = last_screen(tr.send_text(user, "1899"))
    assert "Попробуйте ещё раз" in rejected.text
    assert not any(d == "vc" for _, d in buttons(rejected))  # still stuck on model_year

    accepted = last_screen(tr.send_text(user, "2015"))
    assert any(d == "vc" for _, d in buttons(accepted))  # now on vehicle_review
    assert "Год выпуска: 2015" in accepted.text


def test_tr_requires_model_year_to_confirm_the_vehicle(tr, ids):
    """Keep button reflects the shared validator too: an invalid draft value
    can never be "kept" to skip past the step."""
    user = make_user(114)
    screen = last_screen(_through_model_selection(tr, user, ids))
    assert not [b for b in buttons(screen) if b[1].startswith("k:model_year")]  # nothing to keep yet
    accepted = last_screen(tr.send_text(user, "2018"))
    assert any(d == "vc" for _, d in buttons(accepted))


def test_tr_date_of_birth_validation_matches_web_checkout(tr, ids):
    today = today_in_georgia()
    future = today.replace(year=today.year + 1).isoformat()
    _, future_error = validate_date_of_birth(future, today=today)
    assert future_error  # sanity: a future date of birth is rejected by the shared validator

    user = make_user(115)
    _through_model_selection(tr, user, ids)
    tr.send_text(user, "2015")  # model_year
    tr.press(user, "vc")  # confirm vehicle -> policyholder
    tr.send_text(user, _name(user.id))
    tr.send_text(user, "AB1234567")
    screen = last_screen(tr.press(user, f"cz:{COUNTRIES.index('Russia')}"))
    assert texts.ASK_DATE_OF_BIRTH.splitlines()[0] in screen.text

    future_date = (today.replace(year=today.year + 1)).strftime("%d.%m.%Y")
    rejected = last_screen(tr.send_text(user, future_date))
    assert "Попробуйте ещё раз" in rejected.text

    accepted = last_screen(tr.send_text(user, "05.03.1990"))
    assert "05.03.1990" in accepted.text or "Email" in accepted.text or "email" in accepted.text.lower()


def test_ge_flow_never_asks_for_date_of_birth(ge, ids):
    user = make_user(116)
    _through_model_selection(ge, user, ids)
    screen = last_screen(ge.press(user, "vc"))
    ge.send_text(user, _name(user.id))
    ge.send_text(user, "AB1234567")
    screen = last_screen(ge.press(user, f"cz:{COUNTRIES.index('Russia')}"))
    assert "дата рождения" not in screen.text.lower()
    assert texts.ASK_DATE_OF_BIRTH.splitlines()[0] not in screen.text


def test_tr_model_year_and_date_of_birth_persist_into_the_canonical_order_fields(tr, ids):
    order = _place_order(tr, make_user(117), ids, model_year="2016", date_of_birth="20.11.1985")
    assert order.model_year == 2016
    assert order.date_of_birth.isoformat() == "1985-11-20"


def test_ge_order_never_gets_a_model_year_or_date_of_birth(ge, ids):
    order = _place_order(ge, make_user(118), ids)
    assert order.model_year is None
    assert order.date_of_birth is None


def test_tr_consolidated_review_shows_model_year_and_date_of_birth(tr, ids):
    user = make_user(119)
    _through_model_selection(tr, user, ids)
    tr.send_text(user, "2017")
    tr.press(user, "vc")
    tr.send_text(user, _name(user.id))
    tr.send_text(user, "AB1234567")
    tr.press(user, f"cz:{COUNTRIES.index('Russia')}")
    tr.send_text(user, "12.06.1988")
    tr.send_text(user, f"client{user.id}@example.com")
    screen = last_screen(tr.send_text(user, "+79991234567"))
    assert "Год выпуска: 2017" in screen.text
    assert "Дата рождения: 12.06.1988" in screen.text


def test_ge_consolidated_review_never_shows_model_year_or_date_of_birth_lines(ge, ids):
    user = make_user(120)
    _through_model_selection(ge, user, ids)
    ge.press(user, "vc")
    ge.send_text(user, _name(user.id))
    ge.send_text(user, "AB1234567")
    ge.press(user, f"cz:{COUNTRIES.index('Russia')}")
    ge.send_text(user, f"client{user.id}@example.com")
    screen = last_screen(ge.send_text(user, "+79991234567"))
    assert "Год выпуска" not in screen.text
    assert "Дата рождения" not in screen.text


def test_tr_model_year_is_editable_from_the_consolidated_review(tr, ids):
    user = make_user(121)
    _through_model_selection(tr, user, ids)
    tr.send_text(user, "2017")
    tr.press(user, "vc")
    tr.send_text(user, _name(user.id))
    tr.send_text(user, "AB1234567")
    tr.press(user, f"cz:{COUNTRIES.index('Russia')}")
    tr.send_text(user, "12.06.1988")
    tr.send_text(user, f"client{user.id}@example.com")
    review = last_screen(tr.send_text(user, "+79991234567"))
    edit_data = dict(buttons(review))[texts.BTN_R_MODEL_YEAR]
    prompt = last_screen(tr.press(user, edit_data))
    assert texts.ASK_MODEL_YEAR.splitlines()[0] in prompt.text
    updated = last_screen(tr.send_text(user, "2020"))
    assert "Год выпуска: 2020" in updated.text
    assert "Дата рождения: 12.06.1988" in updated.text  # untouched by editing the other field

    order = _finish_confirm(tr, user)
    assert order.model_year == 2020


def test_tr_date_of_birth_is_editable_from_the_consolidated_review(tr, ids):
    user = make_user(122)
    _through_model_selection(tr, user, ids)
    tr.send_text(user, "2017")
    tr.press(user, "vc")
    tr.send_text(user, _name(user.id))
    tr.send_text(user, "AB1234567")
    tr.press(user, f"cz:{COUNTRIES.index('Russia')}")
    tr.send_text(user, "12.06.1988")
    tr.send_text(user, f"client{user.id}@example.com")
    review = last_screen(tr.send_text(user, "+79991234567"))
    edit_data = dict(buttons(review))[texts.BTN_R_DATE_OF_BIRTH]
    prompt = last_screen(tr.press(user, edit_data))
    assert texts.ASK_DATE_OF_BIRTH.splitlines()[0] in prompt.text
    updated = last_screen(tr.send_text(user, "01.01.1975"))
    assert "Дата рождения: 01.01.1975" in updated.text
    assert "Год выпуска: 2017" in updated.text  # untouched by editing the other field

    order = _finish_confirm(tr, user)
    assert order.date_of_birth.isoformat() == "1975-01-01"


def _finish_confirm(h, user):
    h.press(user, "fc:confirm")
    conn = _conn(h.dispatcher["settings"])
    try:
        return list_telegram_orders(conn, bot_key=h.config.profile.bot_key, telegram_user_id=user.id)[0]
    finally:
        conn.close()


# ======================================= 10. TRUCK / CAMPER (unpriced -> priced)


def test_customer_sees_unavailable_state_for_the_unpriced_truck_camper_category(tr):
    user = make_user(130)
    tr.send_text(user, "/start")
    screen = last_screen(tr.press(user, "c:bus"))
    assert texts.NO_PERIODS in screen.text
    # never a broken/period selection screen for an unpriced category
    assert not [d for _, d in buttons(screen) if d.startswith("p:bus:")]


def test_truck_camper_becomes_purchasable_immediately_after_a_manager_sets_a_price(tr, ids):
    customer = make_user(131)
    tr.send_text(customer, "/start")
    blocked = last_screen(tr.press(customer, "c:bus"))
    assert texts.NO_PERIODS in blocked.text

    owner = make_user(OWNER_TR)
    tr.press(owner, PricesCb(a="menu").pack())
    tr.press(owner, PricesCb(a="cat", c="bus").pack())
    tr.press(owner, PricesCb(a="period", c="bus", p="30d").pack())
    confirm_screen = last_screen(tr.send_text(owner, "2499"))
    confirm_data = dict(buttons(confirm_screen))[texts.BTN_PRICES_CONFIRM]
    tr.press(owner, confirm_data)

    # same customer, no restart: the category is now purchasable
    screen = last_screen(tr.press(customer, "c:bus"))
    assert [d for _, d in buttons(screen) if d.startswith("p:bus:30d")]
    assert texts.NO_PERIODS not in screen.text


def test_truck_camper_unpriced_state_does_not_affect_ge_bus_which_is_already_priced(ge):
    screen = last_screen(ge.press(make_user(132), "c:bus"))
    assert texts.NO_PERIODS not in screen.text  # GE's "Автобус" is a normal, already-priced category


# ============================== 11. TR FIXED CONTACTS (no prompt, defaults)


def test_tr_order_persists_the_fixed_default_contacts(tr, ids):
    order = _place_order(tr, make_user(140), ids)
    assert order.contact_email == "tplgee@mail.ru"
    assert order.contact_phone == "+995574220625"


def test_tr_customer_is_never_asked_for_email_or_phone(tr, ids):
    """Reaches the consolidated review without ever seeing ASK_EMAIL/
    ASK_PHONE -- fixed_contacts() (profile.customer_email/customer_phone)
    skips both steps entirely, same existing mechanism GE's own fixed-
    contact bots already use (see tests/test_telegram_bot_fixed_contacts.py)."""
    user = make_user(141)
    _through_model_selection(tr, user, ids)
    tr.send_text(user, "2015")  # model_year
    tr.press(user, "vc")
    tr.send_text(user, _name(user.id))
    tr.send_text(user, "AB1234567")
    screen = last_screen(tr.press(user, f"cz:{COUNTRIES.index('Russia')}"))
    # citizenship -> date_of_birth directly (no email/phone in between either)
    assert texts.ASK_DATE_OF_BIRTH.splitlines()[0] in screen.text
    review = last_screen(tr.send_text(user, "12.06.1988"))  # -> straight to review, no contact prompts
    assert texts.ASK_EMAIL.splitlines()[0] not in review.text
    assert texts.ASK_PHONE.splitlines()[0] not in review.text
    # _contact_lines() (app.telegram_bot.steps) omits email/phone from the
    # review text entirely for a fixed-contact bot -- persistence into the
    # order itself is proven separately by
    # test_tr_order_persists_the_fixed_default_contacts.


def test_ge_contact_behavior_is_unaffected_by_tr_fixed_contacts(ge, ids):
    """GE_PROFILE here still has no fixed contacts configured -- unchanged."""
    order = _place_order(ge, make_user(142), ids)
    assert order.contact_email == f"client{142}@example.com"
    assert order.contact_phone == "+79991234567"


# ========================== 12. TR START-DATE AUTO-ASSIGNMENT (no prompt)


def test_tr_never_shows_the_start_date_question(tr, ids):
    user = make_user(150)
    tr.send_text(user, "/start")
    screen = last_screen(tr.press(user, "p:passenger_car:30d"))
    assert texts.DATE_PROMPT not in screen.text
    assert not [d for _, d in buttons(screen) if d.startswith("d:")]
    # landed straight on the manual/documents method screen instead
    assert screen.text != texts.DATE_PROMPT


def test_ge_still_shows_the_start_date_question_unchanged(ge, ids):
    user = make_user(151)
    ge.send_text(user, "/start")
    screen = last_screen(ge.press(user, "p:passenger_car:30d"))
    assert screen.text == texts.DATE_PROMPT
    assert ("Сегодня", "d:today") in buttons(screen)
    assert ("Завтра", "d:tomorrow") in buttons(screen)


def test_tr_order_gets_todays_start_date_automatically(tr, ids):
    from app.dates.rules import today_in_georgia

    order = _place_order(tr, make_user(152), ids)
    assert order.start_date == today_in_georgia()


def test_tr_consolidated_review_has_no_start_date_edit_control(tr, ids):
    user = make_user(153)
    _through_model_selection(tr, user, ids)
    tr.send_text(user, "2016")
    tr.press(user, "vc")
    tr.send_text(user, _name(user.id))
    tr.send_text(user, "AB1234567")
    tr.press(user, f"cz:{COUNTRIES.index('Russia')}")
    review = last_screen(tr.send_text(user, "12.06.1988"))
    assert texts.BTN_R_START not in dict(buttons(review))
    # the citizenship edit button is still there, just alone in its row
    assert texts.BTN_R_CITIZENSHIP in dict(buttons(review))


def test_ge_consolidated_review_still_has_the_start_date_edit_control(ge, ids):
    order = _place_order(ge, make_user(154), ids)
    assert order is not None  # sanity: GE's flow (incl. its date step) still completes
    user = make_user(155)
    ge.press(user, "p:passenger_car:30d")
    ge.press(user, "d:tomorrow")
    ge.press(user, "e:manual")
    ge.send_text(user, "AB123CD")
    ge.send_text(user, VIN)
    ge.press(user, f"mf:{ids['TOYOTA']}")
    ge.press(user, f"md:{ids['TOYOTA/CAMRY']}")
    ge.press(user, "vc")
    ge.send_text(user, _name(user.id))
    ge.send_text(user, "AB1234567")
    ge.press(user, f"cz:{COUNTRIES.index('Russia')}")
    ge.send_text(user, f"client{user.id}@example.com")
    review = last_screen(ge.send_text(user, "+79991234567"))
    assert texts.BTN_R_START in dict(buttons(review))
