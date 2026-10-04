"""💰 Цены -- global retail price management inside the SAME bot customers use.

Reuses the canonical pricing stack directly (app.pricing.overrides /
app.pricing.provider) -- there is exactly one effective price per (country,
category, period), shared with the website checkout and this bot's own
checkout (app.checkout.service). No Telegram-only price storage exists or
should ever exist here.

Country-scoped by the CALLING BOT's own profile.country_code -- a bot
profiled for one country edits that country's overrides, and never sees or
touches another country's rows (insurance_price_overrides.country_code, see
app.pricing.overrides). AM has no override-table support at all (it derives
its own linear pricing from config.yaml's duration_ranges.AM, a different
pricing shape entirely -- see app.pricing.provider.resolve_duration_price);
a bot profiled for AM would simply have nothing to edit here.

Category availability/labels shown are this bot's own (see
app.telegram_bot.categories) -- NOT the country-level website list -- so a
bot's own product set (e.g. a relabeled category under a different product
name) is exactly what 💰 Цены offers, never more.

The website's own /admin/prices (app.web.admin_prices_routes) is unrelated
and unaffected by any of this -- it remains hardcoded to GE only (its own
PRICES_COUNTRY_CODE constant), by design, out of this module's scope.

Authorization: both 'owner' and 'manager' may view AND edit (see
app.telegram_bot.staff.role_of) -- price editing is explicitly NOT
owner-only. Checked server-side on every callback/message, exactly like
every other staff action in this project (the keyboard only decides what is
shown).

Existing orders are never touched: app.checkout.service.
create_order_from_draft snapshots price_customer_minor onto the order at
creation time -- a price change here only affects what a NEW draft/order
computes from this moment on.

Pending text input (category/period picked, waiting for the typed RUB
amount) lives in telegram_manager_price_contexts (app.db), one row per
(bot, manager) -- the SAME pattern as telegram_manager_upload_contexts
(app.telegram_bot.managers' policy-PDF upload context), and for the same
reason: a manager can also be a customer with their OWN checkout FSM state
in flight, so this must never reuse that FSM/conversation_state slot (see
app.telegram_bot.storage -- it explicitly rejects any business data there
anyway), and a bot restart must not lose or misattribute the intent.

Multi-manager safety: the confirmation screen for a newly typed price
carries BOTH the price shown as "Было" and the new typed value in its own
callback payload (PricesCb.o / .n). on_prices (action="confirm") re-reads
the CURRENT effective price at the moment "✅ Изменить" is pressed and
refuses to write if it no longer matches "Было" -- see texts.PRICES_STALE.
Reset-to-default (action="reset") is protected the same way, comparing
against the current override's price rather than the config default.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.pricing import overrides as price_overrides
from app.pricing.overrides import parse_price
from app.pricing.provider import PeriodOption, available_periods, config_periods, get_period
from app.settings import Settings
from app.telegram_bot import categories, staff, texts
from app.telegram_bot.keyboards import PricesCb, StaffCb
from app.telegram_bot.profile import BotProfile

logger = logging.getLogger(__name__)

# Same 30-minute window as telegram_manager_upload_contexts
# (app.telegram_bot.managers.UPLOAD_CONTEXT_MINUTES) -- not shared as one
# constant on purpose: the two contexts are unrelated features that happen
# to use the same round number today.
PRICE_CONTEXT_MINUTES = 30


# ----------------------------------------------------------- price context


def set_price_context(
    conn: sqlite3.Connection, *, bot_key: str, country_code: str, manager_user_id: int, category_code: str, period_code: str
) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO telegram_manager_price_contexts
               (bot_key, manager_user_id, country_code, vehicle_category_code, period_code, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (bot_key, manager_user_id) DO UPDATE SET
               country_code = excluded.country_code,
               vehicle_category_code = excluded.vehicle_category_code,
               period_code = excluded.period_code,
               created_at = excluded.created_at,
               expires_at = excluded.expires_at""",
        (
            bot_key, manager_user_id, country_code, category_code, period_code,
            now.isoformat(), (now + timedelta(minutes=PRICE_CONTEXT_MINUTES)).isoformat(),
        ),
    )
    conn.commit()


def get_price_context(conn: sqlite3.Connection, *, bot_key: str, manager_user_id: int) -> tuple[str, str, bool] | None:
    """(category_code, period_code, expired) or None."""
    row = conn.execute(
        """SELECT vehicle_category_code, period_code, expires_at
           FROM telegram_manager_price_contexts WHERE bot_key = ? AND manager_user_id = ?""",
        (bot_key, manager_user_id),
    ).fetchone()
    if row is None:
        return None
    expired = datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc)
    return row["vehicle_category_code"], row["period_code"], expired


def clear_price_context(conn: sqlite3.Connection, *, bot_key: str, manager_user_id: int) -> None:
    conn.execute(
        "DELETE FROM telegram_manager_price_contexts WHERE bot_key = ? AND manager_user_id = ?",
        (bot_key, manager_user_id),
    )
    conn.commit()


# ------------------------------------------------------------------ labels


def _period_label(settings: Settings, country_code: str, category_code: str, period_code: str) -> str:
    return next(
        (p.label for p in config_periods(settings, country_code, category_code) if p.code == period_code),
        period_code,
    )


def _price_line(label: str, price_rub: int | None) -> str:
    # price_rub is None for a period that exists but has no price configured
    # yet (see config.yaml's own docstring) -- never shown as 0.
    return texts.price_line(label, price_rub) if price_rub is not None else f"{label} — не задана"


def _default_price(settings: Settings, country_code: str, category_code: str, period_code: str) -> int | None:
    return next(
        (p.price_rub for p in config_periods(settings, country_code, category_code) if p.code == period_code),
        None,
    )


def _override(
    conn: sqlite3.Connection, country_code: str, category_code: str, period_code: str
) -> price_overrides.PriceOverride | None:
    overrides = {
        (o.vehicle_category_code, o.period_code): o for o in price_overrides.list_overrides(conn, country_code)
    }
    return overrides.get((category_code, period_code))


# ------------------------------------------------------------------ markup


def _markup(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data.pack()) for text, data in row]
            for row in rows
            if row
        ]
    )


async def _show(callback: CallbackQuery, text: str, rows) -> None:
    markup = _markup(rows)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)


# ------------------------------------------------------------------ screens


def menu_screen(conn: sqlite3.Connection, settings: Settings, profile: BotProfile) -> tuple[str, list]:
    """The full current price matrix as text (read-only, like /admin/prices'
    own table) plus one button per category to start editing it. Categories
    and labels are THIS BOT's own (see app.telegram_bot.categories)."""
    cc = profile.country_code
    offered = [
        code for code in categories.offered_category_codes(conn, settings, profile) if config_periods(settings, cc, code)
    ]
    sections = []
    for code in offered:
        periods = config_periods(settings, cc, code)
        effective = {p.code: p for p in available_periods(settings, cc, code)}
        lines = [_price_line(p.label, effective[p.code].price_rub) for p in periods]
        sections.append((categories.category_label(conn, profile, code), lines))
    text = (
        texts.prices_matrix_text(sections, title=texts.prices_title(cc)) if sections else texts.PRICES_NO_CATEGORIES
    )
    rows = [[(categories.category_label(conn, profile, code), PricesCb(a="cat", c=code))] for code in offered]
    rows.append([(texts.BTN_STAFF_BACK, StaffCb(a="menu"))])
    return text, rows


def category_screen(
    conn: sqlite3.Connection, settings: Settings, profile: BotProfile, category_code: str, *, notice: str | None = None
) -> tuple[str, list]:
    cc = profile.country_code
    offered = set(categories.offered_category_codes(conn, settings, profile))
    if category_code not in offered:
        return texts.PRICES_CATEGORY_UNKNOWN, [[(texts.BTN_STAFF_BACK, PricesCb(a="menu"))]]
    periods = config_periods(settings, cc, category_code)
    if not periods:
        return texts.PRICES_NO_PERIODS, [[(texts.BTN_STAFF_BACK, PricesCb(a="menu"))]]
    effective = {p.code: p for p in available_periods(settings, cc, category_code)}
    rows = [
        [(_price_line(p.label, effective[p.code].price_rub), PricesCb(a="period", c=category_code, p=p.code))]
        for p in periods
    ]
    rows.append([(texts.BTN_STAFF_BACK, PricesCb(a="menu"))])
    text = categories.category_label(conn, profile, category_code)
    return (f"{notice}\n\n{text}" if notice else text), rows


def period_prompt_screen(
    conn: sqlite3.Connection, settings: Settings, profile: BotProfile, category_code: str, period_code: str
) -> tuple[str, list]:
    cc = profile.country_code
    period: PeriodOption | None = get_period(settings, cc, category_code, period_code)
    current_rub = period.price_rub if period else None
    rows = []
    if _override(conn, cc, category_code, period_code) is not None:
        rows.append([(texts.BTN_PRICES_RESET, PricesCb(a="reset_ask", c=category_code, p=period_code))])
    rows.append([(texts.BTN_PRICES_CANCEL_INPUT, PricesCb(a="cancel", c=category_code, p=period_code))])
    rows.append([(texts.BTN_STAFF_BACK, PricesCb(a="cat", c=category_code))])
    return texts.prices_current_price_prompt(current_rub), rows


def reset_confirm_screen(
    conn: sqlite3.Connection, settings: Settings, profile: BotProfile, category_code: str, period_code: str
) -> tuple[str, list] | None:
    """None -- nothing to reset (no override exists for this cell)."""
    cc = profile.country_code
    existing = _override(conn, cc, category_code, period_code)
    if existing is None:
        return None
    default_rub = _default_price(settings, cc, category_code, period_code)
    text = texts.prices_reset_confirm(
        category_label=categories.category_label(conn, profile, category_code),
        period_label=_period_label(settings, cc, category_code, period_code),
        current_rub=existing.price_rub, default_rub=default_rub,
    )
    rows = [
        [(texts.BTN_PRICES_RESET_CONFIRM, PricesCb(a="reset", c=category_code, p=period_code, o=existing.price_rub))],
        [(texts.BTN_PRICES_CANCEL_INPUT, PricesCb(a="period", c=category_code, p=period_code))],
    ]
    return text, rows


# ----------------------------------------------------------------- actions


async def _confirm(
    callback: CallbackQuery, callback_data: PricesCb, conn: sqlite3.Connection, settings: Settings, profile: BotProfile
) -> None:
    cc = profile.country_code
    category_code, period_code, new_price = callback_data.c, callback_data.p, callback_data.n
    shown_old = callback_data.o or None  # 0 is the "was not priced yet" sentinel -- real prices are always > 0
    current = get_period(settings, cc, category_code, period_code)
    if current is None:
        await callback.answer(texts.PRICES_CATEGORY_UNKNOWN, show_alert=True)
        await _show(callback, *menu_screen(conn, settings, profile))
        return
    if current.price_rub != shown_old:
        # Another manager changed this cell between the confirmation screen
        # being shown and "✅ Изменить" being pressed -- never overwrite blindly.
        await callback.answer(texts.PRICES_STALE, show_alert=True)
        await _show(callback, *category_screen(conn, settings, profile, category_code))
        return

    # Same "telegram manager <id>" shape as app.telegram_bot.managers'
    # payment confirm/reject actor -- one audit convention for every
    # manager-attributed write in this bot, never the (unverified, often
    # absent) Telegram username.
    updated_by = f"telegram manager {callback.from_user.id}"
    price_overrides.upsert_override(
        conn, country_code=cc, vehicle_category_code=category_code,
        period_code=period_code, price_rub=new_price, updated_by=updated_by,
    )
    conn.commit()
    logger.info(
        "Retail price changed: %s/%s/%s %s -> %s RUB by telegram manager %s (bot_key=%s)",
        cc, category_code, period_code, current.price_rub, new_price, callback.from_user.id, profile.bot_key,
    )
    notice = texts.prices_changed(
        category_label=categories.category_label(conn, profile, category_code),
        period_label=_period_label(settings, cc, category_code, period_code),
        old_rub=current.price_rub, new_rub=new_price,
    )
    await callback.answer()
    await _show(callback, *category_screen(conn, settings, profile, category_code, notice=notice))


async def _reset(
    callback: CallbackQuery, callback_data: PricesCb, conn: sqlite3.Connection, settings: Settings, profile: BotProfile
) -> None:
    cc = profile.country_code
    category_code, period_code = callback_data.c, callback_data.p
    shown_old = callback_data.o or None
    existing = _override(conn, cc, category_code, period_code)
    existing_price = existing.price_rub if existing else None
    if existing_price != shown_old:
        await callback.answer(texts.PRICES_STALE, show_alert=True)
        await _show(callback, *category_screen(conn, settings, profile, category_code))
        return

    if existing is not None:
        price_overrides.delete_override(
            conn, country_code=cc, vehicle_category_code=category_code, period_code=period_code
        )
        conn.commit()
        logger.info(
            "Retail price override removed: %s/%s/%s by telegram manager %s (config default applies, bot_key=%s)",
            cc, category_code, period_code, callback.from_user.id, profile.bot_key,
        )
    notice = texts.prices_reset_done(
        category_label=categories.category_label(conn, profile, category_code),
        period_label=_period_label(settings, cc, category_code, period_code),
        default_rub=_default_price(settings, cc, category_code, period_code),
    )
    await callback.answer()
    await _show(callback, *category_screen(conn, settings, profile, category_code, notice=notice))


# ----------------------------------------------------------------- handler


async def on_prices(
    callback: CallbackQuery, callback_data: PricesCb, conn: sqlite3.Connection, settings: Settings, profile: BotProfile
) -> None:
    user_id = callback.from_user.id
    role = staff.role_of(conn, profile.bot_key, user_id)
    action, category_code, period_code = callback_data.a, callback_data.c, callback_data.p
    if role is None:
        logger.warning("Rejected prices screen %r from a non-staff user", action)
        await callback.answer(texts.MGR_NO_ACCESS, show_alert=True)
        return
    # Price editing is explicitly NOT owner-only (see module docstring) --
    # no OWNER_ACTIONS-style check here, unlike app.telegram_bot.staff_panel.
    staff.touch_username(conn, profile.bot_key, user_id, callback.from_user.username)

    if action == "menu":
        clear_price_context(conn, bot_key=profile.bot_key, manager_user_id=user_id)
        await _show(callback, *menu_screen(conn, settings, profile))
    elif action == "cat":
        clear_price_context(conn, bot_key=profile.bot_key, manager_user_id=user_id)
        await _show(callback, *category_screen(conn, settings, profile, category_code))
    elif action == "period":
        offered = set(categories.offered_category_codes(conn, settings, profile))
        valid_period = category_code in offered and any(
            p.code == period_code for p in config_periods(settings, profile.country_code, category_code)
        )
        if not valid_period:
            await _show(callback, *category_screen(conn, settings, profile, category_code))
        else:
            set_price_context(
                conn, bot_key=profile.bot_key, country_code=profile.country_code, manager_user_id=user_id,
                category_code=category_code, period_code=period_code,
            )
            await _show(callback, *period_prompt_screen(conn, settings, profile, category_code, period_code))
    elif action == "cancel":
        clear_price_context(conn, bot_key=profile.bot_key, manager_user_id=user_id)
        await _show(callback, *category_screen(conn, settings, profile, category_code, notice=texts.PRICES_INPUT_CANCELLED))
    elif action == "reset_ask":
        screen = reset_confirm_screen(conn, settings, profile, category_code, period_code)
        await _show(callback, *(screen if screen is not None else category_screen(conn, settings, profile, category_code)))
    elif action == "reset":
        await _reset(callback, callback_data, conn, settings, profile)
        return  # answers itself
    elif action == "confirm":
        await _confirm(callback, callback_data, conn, settings, profile)
        return  # answers itself
    await callback.answer()


async def on_prices_text(message: Message, conn: sqlite3.Connection, settings: Settings, profile: BotProfile) -> None:
    """The next plain-text message from a manager with an active price
    context (see set_price_context, fired by on_prices action="period") is
    the new RUB price for that cell. Anything else -- a non-staff sender, no
    active context, or a slash command (so /start etc. keep working for a
    manager who got sidetracked) -- falls through to the normal (customer)
    handlers unchanged, exactly like app.telegram_bot.managers.
    on_manager_document does for an unrelated document."""
    cc = profile.country_code
    user_id = message.from_user.id
    role = staff.role_of(conn, profile.bot_key, user_id)
    if role is None:
        raise SkipHandler()
    if (message.text or "").startswith("/"):
        clear_price_context(conn, bot_key=profile.bot_key, manager_user_id=user_id)
        raise SkipHandler()
    context = get_price_context(conn, bot_key=profile.bot_key, manager_user_id=user_id)
    if context is None:
        raise SkipHandler()
    category_code, period_code, expired = context
    if expired:
        clear_price_context(conn, bot_key=profile.bot_key, manager_user_id=user_id)
        await message.answer(texts.PRICES_INPUT_EXPIRED)
        return

    price, error = parse_price(message.text)
    if error:
        await message.answer(error)  # context kept: the manager can just retype it
        return

    clear_price_context(conn, bot_key=profile.bot_key, manager_user_id=user_id)
    current = get_period(settings, cc, category_code, period_code)
    if current is None:
        await message.answer(texts.PRICES_CATEGORY_UNKNOWN)
        return

    text = texts.prices_confirm_prompt(
        category_label=categories.category_label(conn, profile, category_code),
        period_label=_period_label(settings, cc, category_code, period_code),
        old_rub=current.price_rub, new_rub=price,
    )
    rows = [
        [(texts.BTN_PRICES_CONFIRM, PricesCb(a="confirm", c=category_code, p=period_code, o=current.price_rub or 0, n=price))],
        [(texts.BTN_PRICES_CANCEL_INPUT, PricesCb(a="cancel", c=category_code, p=period_code))],
    ]
    await message.answer(text, reply_markup=_markup(rows))


def register(router: Router) -> None:
    router.callback_query.register(on_prices, PricesCb.filter())
    router.message.register(on_prices_text, F.text)
