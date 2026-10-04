"""Tests for 💰 Цены -- global retail price management inside the SAME
Telegram bot customers use (app.telegram_bot.staff_prices).

These assert there is exactly ONE effective price, shared with the web
checkout and this bot's own checkout (app.pricing.overrides/provider) --
never a parallel Telegram-only price store. All offline: fake Bot API
session, tmp DB, real config.yaml GE prices (passenger_car 15d/30d/90d =
1349/2149/3649, confirmed in config/config.yaml)."""

import dataclasses
import re

import pytest
from aiogram.methods import AnswerCallbackQuery

from app.countries import COUNTRIES
from app.db import get_connection
from app.deps import PROJECT_ROOT
from app.orders.repository import get_order_by_id, list_telegram_orders
from app.pricing import overrides as price_overrides
from app.pricing.provider import available_periods, get_period
from app.settings import load_settings
from app.telegram_bot import texts
from app.telegram_bot.keyboards import PricesCb, StaffCb
from telegram_bot_helpers import TEST_PROFILE, BotHarness, buttons, last_screen, make_user, seed_catalog

OWNER = 999
VIN = "WVWZZZ1JZXW000001"
PROFILE = dataclasses.replace(
    TEST_PROFILE, username="OsagoTestBot", customer_email="tplgee@mail.ru", customer_phone="+995 574 22 06 25"
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TELEGRAM_PAYMENT_BANK_NAME", "Сбербанк")
    monkeypatch.setenv("TELEGRAM_PAYMENT_PHONE_NUMBER", "+79495205223")
    monkeypatch.setenv("TELEGRAM_PAYMENT_RECIPIENT", "Владимир М.")
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def ids(settings):
    return seed_catalog(settings.app.db_file)


@pytest.fixture
def h(settings, ids):
    harness = BotHarness(settings, profile=PROFILE, manager_ids={OWNER})
    yield harness
    harness.close()


def _conn(settings):
    return get_connection(settings.app.db_file)


def _overrides(settings):
    conn = _conn(settings)
    try:
        return {(o.vehicle_category_code, o.period_code): o for o in price_overrides.list_overrides(conn, "GE")}
    finally:
        conn.close()


def _set_override(settings, *, category="passenger_car", period="30d", price, by="t"):
    conn = _conn(settings)
    try:
        price_overrides.upsert_override(
            conn, country_code="GE", vehicle_category_code=category, period_code=period, price_rub=price, updated_by=by
        )
        conn.commit()
    finally:
        conn.close()


def _invite_link(h) -> str:
    screen = last_screen(h.press(make_user(OWNER), "st:invite:0:0"))
    (link,) = re.findall(r"https://t\.me/OsagoTestBot\?start=mgr_[A-Za-z0-9_-]+", screen.text)
    return link


def _add_manager(h, user_id: int) -> "make_user":
    token = _invite_link(h).split("start=", 1)[1]
    user = make_user(user_id, f"mgr{user_id}")
    h.send_text(user, f"/start {token}")
    return user


def _open_prices(h, user):
    h.send_text(user, "/start")
    return h.press(user, StaffCb(a="menu").pack()) and last_screen(h.press(user, PricesCb(a="menu").pack()))


def _alerts(calls):
    return [c for c in calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]


def _name(user_id: int) -> str:
    return "Client " + "".join(chr(ord("A") + int(d)) for d in str(user_id))


def _place_order(h, user, ids, plate="AB123CD"):
    """A real order through the actual checkout conversation -- the same
    path a paying customer uses -- so its price_customer_minor is genuinely
    snapshotted by app.checkout.service.create_order_from_draft, not
    fabricated by the test."""
    for step in (
        lambda: h.send_text(user, "/start"),
        lambda: h.press(user, "p:passenger_car:30d"),
        lambda: h.press(user, "d:tomorrow"),
        lambda: h.press(user, "e:manual"),
        lambda: h.send_text(user, plate),
        lambda: h.send_text(user, VIN),
        lambda: h.press(user, f"mf:{ids['TOYOTA']}"),
        lambda: h.press(user, f"md:{ids['TOYOTA/CAMRY']}"),
        lambda: h.press(user, "vc"),
        lambda: h.send_text(user, _name(user.id)),
        lambda: h.send_text(user, "AB1234567"),
        lambda: h.press(user, f"cz:{COUNTRIES.index('Russia')}"),
    ):
        step()
    h.press(user, "fc:confirm")
    conn = _conn(h.dispatcher["settings"])
    try:
        return list_telegram_orders(conn, bot_key=PROFILE.bot_key, telegram_user_id=user.id)[0]
    finally:
        conn.close()


# ------------------------------------------------------------------ access


def test_manager_sees_prices_button(h):
    manager = _add_manager(h, 20)
    screen = last_screen(h.send_text(manager, "/start"))
    assert texts.BTN_STAFF_PRICES in [t for t, _ in buttons(screen)]


def test_owner_sees_prices_button(h):
    screen = last_screen(h.send_text(make_user(OWNER), "/start"))
    assert texts.BTN_STAFF_PRICES in [t for t, _ in buttons(screen)]


def test_customer_does_not_see_prices_button(h):
    screen = last_screen(h.send_text(make_user(10), "/start"))
    assert texts.BTN_STAFF_PRICES not in [t for t, _ in buttons(screen)]
    assert not any(d.startswith("pr:") for _, d in buttons(screen))


def test_unauthorized_callback_is_rejected(h):
    customer = make_user(11)
    h.send_text(customer, "/start")
    for data in (
        PricesCb(a="menu").pack(), PricesCb(a="cat", c="passenger_car").pack(),
        PricesCb(a="period", c="passenger_car", p="30d").pack(),
        PricesCb(a="confirm", c="passenger_car", p="30d", o=1349, n=1999).pack(),
    ):
        calls = h.press(customer, data)
        (alert,) = _alerts(calls)
        assert alert.text == texts.MGR_NO_ACCESS, data
    # nothing was changed by the attacker
    assert _overrides(h.dispatcher["settings"]) == {}


def test_unauthorized_text_does_not_act_as_a_price(h):
    """A customer typing a number must never be mistaken for a price --
    there is no active price context for them."""
    customer = make_user(11)
    h.send_text(customer, "/start")
    h.send_text(customer, "1999")
    assert _overrides(h.dispatcher["settings"]) == {}


# ---------------------------------------------------------------- display


def test_menu_shows_current_effective_prices(h, settings):
    screen = _open_prices(h, make_user(OWNER))
    assert texts.PRICES_TITLE in screen.text
    assert "🚗 Легковой автомобиль" in screen.text
    assert "15 дней — 1 349 ₽" in screen.text
    assert "30 дней — 2 149 ₽" in screen.text
    assert "90 дней — 3 649 ₽" in screen.text


def test_menu_reflects_an_existing_override(h, settings):
    _set_override(settings, price=1999)
    screen = _open_prices(h, make_user(OWNER))
    assert "30 дней — 1 999 ₽" in screen.text
    assert "15 дней — 1 349 ₽" in screen.text  # untouched cell: real config price


def test_category_selection_shows_its_periods(h):
    owner = make_user(OWNER)
    _open_prices(h, owner)
    screen = last_screen(h.press(owner, PricesCb(a="cat", c="passenger_car").pack()))
    labels = {t: d for t, d in buttons(screen)}
    assert "15 дней — 1 349 ₽" in labels
    assert "30 дней — 2 149 ₽" in labels
    assert "90 дней — 3 649 ₽" in labels
    assert texts.BTN_STAFF_BACK in labels


def test_period_selection_shows_current_price_prompt(h):
    owner = make_user(OWNER)
    _open_prices(h, owner)
    h.press(owner, PricesCb(a="cat", c="passenger_car").pack())
    screen = last_screen(h.press(owner, PricesCb(a="period", c="passenger_car", p="30d").pack()))
    assert screen.text == "Текущая цена:\n2 149 ₽\n\nВведите новую цену в рублях."
    # No override exists yet: no reset button offered.
    assert texts.BTN_PRICES_RESET not in [t for t, _ in buttons(screen)]


def test_reset_button_offered_only_when_an_override_exists(h, settings):
    _set_override(settings, price=1999)
    owner = make_user(OWNER)
    _open_prices(h, owner)
    h.press(owner, PricesCb(a="cat", c="passenger_car").pack())
    screen = last_screen(h.press(owner, PricesCb(a="period", c="passenger_car", p="30d").pack()))
    assert texts.BTN_PRICES_RESET in [t for t, _ in buttons(screen)]


# -------------------------------------------------------------- new price


def _reach_period(h, user):
    _open_prices(h, user)
    h.press(user, PricesCb(a="cat", c="passenger_car").pack())
    h.press(user, PricesCb(a="period", c="passenger_car", p="30d").pack())


def test_valid_price_parsing_shows_confirmation(h):
    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    screen = last_screen(h.send_text(manager, "1 499"))
    assert "Изменить глобальную цену?" in screen.text
    assert "Было: 2 149 ₽" in screen.text
    assert "Будет: 1 499 ₽" in screen.text
    assert texts.BTN_PRICES_CONFIRM in [t for t, _ in buttons(screen)]
    assert texts.BTN_PRICES_CANCEL_INPUT in [t for t, _ in buttons(screen)]


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "", "1.5"])
def test_malformed_or_nonpositive_price_is_rejected(h, settings, raw):
    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    h.send_text(manager, raw)
    assert _overrides(settings) == {}


def test_rejected_price_keeps_the_context_so_the_manager_can_retry(h, settings):
    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    h.send_text(manager, "abc")
    screen = last_screen(h.send_text(manager, "1 499"))
    assert "Будет: 1 499 ₽" in screen.text


def test_cancel_makes_no_change(h, settings):
    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    confirm_screen = last_screen(h.send_text(manager, "1499"))
    cancel_data = dict(buttons(confirm_screen))[texts.BTN_PRICES_CANCEL_INPUT]
    h.press(manager, cancel_data)
    assert _overrides(settings) == {}
    assert get_period(settings, "GE", "passenger_car", "30d").price_rub == 2149


def test_confirm_updates_the_canonical_global_price(h, settings):
    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    confirm_screen = last_screen(h.send_text(manager, "1499"))
    confirm_data = dict(buttons(confirm_screen))[texts.BTN_PRICES_CONFIRM]
    screen = last_screen(h.press(manager, confirm_data))

    assert "✅ Цена изменена" in screen.text
    assert "2 149 ₽ → 1 499 ₽" in screen.text
    overrides = _overrides(settings)
    assert overrides[("passenger_car", "30d")].price_rub == 1499
    assert overrides[("passenger_car", "30d")].updated_by == f"telegram manager {manager.id}"


def test_website_pricing_service_sees_the_same_new_price(h, settings):
    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    confirm_screen = last_screen(h.send_text(manager, "1499"))
    h.press(manager, dict(buttons(confirm_screen))[texts.BTN_PRICES_CONFIRM])

    # The exact function app.web.admin_prices_routes / the web checkout use.
    periods = {p.code: p.price_rub for p in available_periods(settings, "GE", "passenger_car")}
    assert periods["30d"] == 1499
    assert periods["15d"] == 1349  # untouched


def test_telegram_checkout_sees_the_same_new_price(h, settings):
    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    confirm_screen = last_screen(h.send_text(manager, "1499"))
    h.press(manager, dict(buttons(confirm_screen))[texts.BTN_PRICES_CONFIRM])

    customer = make_user(50)
    h.send_text(customer, "/start")
    screen = last_screen(h.press(customer, "c:passenger_car"))
    assert "30 дней — 1 499 ₽" in screen.text


def test_existing_order_price_remains_unchanged_after_a_global_price_change(h, settings, ids):
    customer = make_user(40)
    order = _place_order(h, customer, ids)
    assert order.price_customer_minor == 214900  # 2 149 RUB, the config default at order time

    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    confirm_screen = last_screen(h.send_text(manager, "1499"))
    h.press(manager, dict(buttons(confirm_screen))[texts.BTN_PRICES_CONFIRM])

    conn = _conn(settings)
    try:
        assert get_order_by_id(conn, order.id).price_customer_minor == 214900
    finally:
        conn.close()


# -------------------------------------------------------------------- reset


def test_reset_to_default_works(h, settings):
    _set_override(settings, price=1999)
    owner = make_user(OWNER)
    _open_prices(h, owner)
    h.press(owner, PricesCb(a="cat", c="passenger_car").pack())
    period_screen = last_screen(h.press(owner, PricesCb(a="period", c="passenger_car", p="30d").pack()))
    reset_ask_data = dict(buttons(period_screen))[texts.BTN_PRICES_RESET]
    confirm_screen = last_screen(h.press(owner, reset_ask_data))
    assert "Вернуть базовую цену?" in confirm_screen.text
    assert "1 999 ₽" in confirm_screen.text  # current (overridden)
    assert "2 149 ₽" in confirm_screen.text  # config default

    reset_data = dict(buttons(confirm_screen))[texts.BTN_PRICES_RESET_CONFIRM]
    screen = last_screen(h.press(owner, reset_data))
    assert "✅ Цена возвращена к базовой" in screen.text
    assert _overrides(settings) == {}
    assert get_period(settings, "GE", "passenger_car", "30d").price_rub == 2149


def test_reset_cancel_keeps_the_override(h, settings):
    _set_override(settings, price=1999)
    owner = make_user(OWNER)
    _open_prices(h, owner)
    h.press(owner, PricesCb(a="cat", c="passenger_car").pack())
    period_screen = last_screen(h.press(owner, PricesCb(a="period", c="passenger_car", p="30d").pack()))
    confirm_screen = last_screen(h.press(owner, dict(buttons(period_screen))[texts.BTN_PRICES_RESET]))
    h.press(owner, dict(buttons(confirm_screen))[texts.BTN_PRICES_CANCEL_INPUT])
    assert _overrides(settings)[("passenger_car", "30d")].price_rub == 1999


# ----------------------------------------------------- multi-manager safety


def test_stale_confirmation_cannot_overwrite_a_newer_price(h, settings):
    manager_a = _add_manager(h, 20)
    manager_b = _add_manager(h, 21)

    _reach_period(h, manager_a)
    confirm_a = last_screen(h.send_text(manager_a, "1499"))
    confirm_a_data = dict(buttons(confirm_a))[texts.BTN_PRICES_CONFIRM]

    # Manager B changes the same cell in between, via the SAME bot (a real
    # concurrent edit), before A presses "✅ Изменить".
    _reach_period(h, manager_b)
    confirm_b = last_screen(h.send_text(manager_b, "1799"))
    h.press(manager_b, dict(buttons(confirm_b))[texts.BTN_PRICES_CONFIRM])
    assert _overrides(settings)[("passenger_car", "30d")].price_rub == 1799

    calls = h.press(manager_a, confirm_a_data)
    (alert,) = _alerts(calls)
    assert alert.text == texts.PRICES_STALE
    # B's price is untouched -- A's stale write never happened.
    assert _overrides(settings)[("passenger_car", "30d")].price_rub == 1799


def test_stale_reset_cannot_overwrite_a_newer_price(h, settings):
    _set_override(settings, price=1999)
    owner = make_user(OWNER)
    _open_prices(h, owner)
    h.press(owner, PricesCb(a="cat", c="passenger_car").pack())
    period_screen = last_screen(h.press(owner, PricesCb(a="period", c="passenger_car", p="30d").pack()))
    reset_confirm = last_screen(h.press(owner, dict(buttons(period_screen))[texts.BTN_PRICES_RESET]))
    reset_data = dict(buttons(reset_confirm))[texts.BTN_PRICES_RESET_CONFIRM]

    # Another manager changes the price in between.
    _set_override(settings, price=2599, by="other")

    calls = h.press(owner, reset_data)
    (alert,) = _alerts(calls)
    assert alert.text == texts.PRICES_STALE
    assert _overrides(settings)[("passenger_car", "30d")].price_rub == 2599


# ------------------------------------------------------------------ restart


def test_restart_preserves_the_override(h, settings):
    manager = _add_manager(h, 20)
    _reach_period(h, manager)
    confirm_screen = last_screen(h.send_text(manager, "1499"))
    h.press(manager, dict(buttons(confirm_screen))[texts.BTN_PRICES_CONFIRM])
    h.close()

    second = BotHarness(settings, profile=PROFILE, manager_ids={OWNER})
    try:
        assert get_period(settings, "GE", "passenger_car", "30d").price_rub == 1499
        screen = _open_prices(second, make_user(OWNER))
        assert "30 дней — 1 499 ₽" in screen.text
    finally:
        second.close()
