"""Inline keyboards + their callback payloads.

Callback data is only ever a *request* -- every handler re-validates it
against the canonical rules and the customer's own draft (never trusting
that a code in a payload is still offered/priced). Payloads stay well under
Telegram's 64-byte limit."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.pricing.provider import PeriodOption
from app.telegram_bot import texts


class MenuCb(CallbackData, prefix="m"):
    action: str  # "apply" | "categories"


class CategoryCb(CallbackData, prefix="c"):
    code: str


class PeriodCb(CallbackData, prefix="p"):
    # The category is carried too, so a stale button from an earlier
    # category's message selects exactly what the customer saw on it.
    category: str
    period: str


class DateCb(CallbackData, prefix="d"):
    choice: str  # "today" | "tomorrow" | "manual" | "back"


class MethodCb(CallbackData, prefix="e"):
    choice: str  # "documents" | "manual"


def intro_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_APPLY, callback_data=MenuCb(action="apply"))
    return builder.as_markup()


def categories_keyboard(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """items: (category_code, label) in display order."""
    builder = InlineKeyboardBuilder()
    for code, label in items:
        builder.button(text=label, callback_data=CategoryCb(code=code))
    builder.adjust(1)
    return builder.as_markup()


def periods_keyboard(category_code: str, periods: list[PeriodOption]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for period in periods:
        builder.button(
            text=texts.price_line(period.label, period.price_rub),
            callback_data=PeriodCb(category=category_code, period=period.code),
        )
    builder.button(text=texts.BTN_BACK, callback_data=MenuCb(action="categories"))
    builder.adjust(1)
    return builder.as_markup()


def date_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_TODAY, callback_data=DateCb(choice="today"))
    builder.button(text=texts.BTN_TOMORROW, callback_data=DateCb(choice="tomorrow"))
    builder.button(text=texts.BTN_MANUAL_DATE, callback_data=DateCb(choice="manual"))
    builder.button(text=texts.BTN_BACK, callback_data=DateCb(choice="back"))
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def method_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_METHOD_DOCUMENTS, callback_data=MethodCb(choice="documents"))
    builder.button(text=texts.BTN_METHOD_MANUAL, callback_data=MethodCb(choice="manual"))
    builder.adjust(1)
    return builder.as_markup()


# ---------------------------------------------------------------- phase 4


class NavCb(CallbackData, prefix="n"):
    # Go to a named step (see app.telegram_bot.steps.STEPS); rt = where an
    # edit should return afterwards ("" = normal forward flow).
    to: str
    rt: str = ""


class KeepCb(CallbackData, prefix="k"):
    step: str  # keep the draft's current value for this input step


class SuggestCb(CallbackData, prefix="sg"):
    step: str  # use the OCR-read suggestion for this input step


class VehicleConfirmCb(CallbackData, prefix="vc"):
    pass


class ManufacturerCb(CallbackData, prefix="mf"):
    id: int


class ModelCb(CallbackData, prefix="md"):
    id: int


class ModelPageCb(CallbackData, prefix="mp"):
    page: int


class CitizenshipCb(CallbackData, prefix="cz"):
    i: int  # index into app.countries.COUNTRIES


class DocsCb(CallbackData, prefix="dc"):
    action: str  # "done" ("recognize" = the same, from older messages) | "discard"


class DocsRetryCb(CallbackData, prefix="dr"):
    # An explicit retry of ONE specific failed batch: seq is that batch's
    # number, so a doubled/queued/stale press can never start a second run.
    seq: int


class FinalCb(CallbackData, prefix="fc"):
    # "confirm": "✅ Всё верно" on the consolidated review -- the final
    # confirmation (creates the order). "continue": the former final
    # review's "✅ Продолжить" on older messages -- handled the same way.
    action: str


def keyboard(rows: list[list[tuple[str, CallbackData]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data.pack()) for text, data in row] for row in rows if row
        ]
    )


# ---------------------------------------------------------------- phase 5


class OrderCb(CallbackData, prefix="o"):
    # Customer action on one of THEIR orders -- the handler always re-checks
    # the order belongs to the pressing user and this bot.
    action: str  # "details" | "receipt" | "resend_policy" | "new"
    order_id: int


class MgrCb(CallbackData, prefix="mg"):
    # Manager action -- never trusted: sender must be a configured manager,
    # the order must be this bot's Telegram order in the expected status.
    # "confirm" | "reject" | "policy" | "resend_policy" | "cancel_upload";
    # "lconfirm" / "lreject": the same confirm/reject pressed on a card
    # opened from a manager list (refreshed in place afterwards).
    action: str
    order_id: int



class NewCheckoutCb(CallbackData, prefix="nc"):
    # "🚗 Новая страховка": abandon the current checkout and start fresh.
    # g = the checkout generation the button was shown for -- a doubled or
    # replayed press (generation already replaced) never resets twice.
    g: str


class StaffCb(CallbackData, prefix="st"):
    # Manager/owner screens. NEVER trusted: the handler re-checks the
    # sender's role in telegram_bot_staff on every press.
    # a: "menu" | "orders" | "pending" | "co"/"cp" (order card opened from
    #    orders / pending) | "receipt" | "managers" | "invite" | "rmlist" |
    #    "rmask" | "rm"
    # i: order id or Telegram user id; p: list page
    a: str
    i: int = 0
    p: int = 0


class IssueCb(CallbackData, prefix="is"):
    # Staff-only operator issuance from the consolidated review:
    # a="ask" -> the explicit "🛡 Оформить полис?" confirmation,
    # a="go"  -> issue. g = the checkout generation the screen was built for
    # (also the order's idempotency key, see app.telegram_bot.operator_issue);
    # h = a fingerprint of the data shown, so a confirmation shown BEFORE an
    # edit can never issue the edited data unseen. Never trusted: the
    # sender's staff role is re-checked on every press.
    a: str
    g: str = ""
    h: str = ""


class OpCb(CallbackData, prefix="op"):
    # Operator order actions: "status" | "retry" | "link" | "paid" | "resend".
    # Staff only (re-checked), the order must be this bot's operator order.
    a: str
    order_id: int


class PricesCb(CallbackData, prefix="pr"):
    # "💰 Цены" -- global retail price management (app.pricing.overrides),
    # the SAME effective price the website checkout and this bot's own
    # checkout use. NEVER trusted: the handler re-checks the sender's staff
    # role (owner OR manager, see app.telegram_bot.staff_prices) on every
    # press, exactly like every other staff callback in this project.
    # a: "menu" | "cat" | "period" | "cancel" | "confirm" | "reset_ask" | "reset"
    # c: vehicle_category_code; p: period_code
    # o/n: old/new price in RUB, carried ONLY on "confirm" (re-validated
    # against the current effective price at confirm time -- see
    # on_prices_confirm -- never written blindly from a stale value here)
    a: str
    c: str = ""
    p: str = ""
    o: int = 0
    n: int = 0
