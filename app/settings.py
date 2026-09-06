"""Configuration loading: config/config.yaml (non-secret structure) + .env (secrets).

Mirrors the split used elsewhere for this kind of project: prices/periods
are checked into config.yaml, while payment requisites, cookie behaviour
and DB path overrides come from environment variables so they never end up
committed to git.
"""

import os
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class ConfigError(Exception):
    pass


class PeriodConfig(BaseModel):
    code: str
    label: str
    # None means "period exists, price not configured yet" — the checkout must
    # show this as a not-available/configuration-required state, never invent
    # a number. See app.pricing.provider.
    price_rub: int | None = None
    # Turkey-only alternative to price_rub: a source (strahovka-turkiye.com)
    # tariff in Turkish Lira, converted at read time via
    # PricingSettings.tr_tl_conversion -- see
    # app.pricing.provider.available_periods, the only consumer. A period
    # never sets both; GE/AM periods never set this at all, so they keep
    # reading price_rub exactly as before. Absent (None) alongside a None
    # price_rub still means "not priced yet", same as always.
    price_tl: int | None = None


class LinearDurationPricingConfig(BaseModel):
    # Two reference points define a straight line (RUB per calendar day);
    # base_price(duration_days) = reference_price_rub_1 +
    # (duration_days - reference_days_1) * slope, where slope is the two
    # points' rate of change. Applied across the WHOLE min_days..max_days
    # range, including days outside [reference_days_1, reference_days_2] --
    # that's an intentional business rule (extrapolation), not a bug -- see
    # app.pricing.provider.resolve_duration_price, the only consumer.
    reference_days_1: int
    reference_price_rub_1: int
    reference_days_2: int
    reference_price_rub_2: int
    # Applied to the rounded base price, in whole percent, 0-100. 0 (default)
    # is a no-op.
    discount_percent: int = 0


class DurationRangeConfig(BaseModel):
    # EXACT DATE RANGE product (currently Armenia's foreign-vehicle CMTPL):
    # the customer picks start_date/end_date directly rather than choosing
    # from a fixed period list -- see app.pricing.provider.get_duration_range
    # and app.web.checkout_routes._parse_duration_range_dates, the only
    # consumers.
    min_days: int
    max_days: int
    # None means "period range exists, price NOT YET CONFIGURED" -- same
    # not-a-number-guess rule as PeriodConfig.price_rub above -- see
    # app.pricing.provider.resolve_duration_price.
    pricing: LinearDurationPricingConfig | None = None
    # Pre-fills end_date (start_date + this many days) the FIRST time a
    # fresh /date draft is shown -- see app.web.checkout_routes.get_date_step,
    # the only consumer. None means "no default configured", preserving
    # today's exact behaviour (blank end_date until the customer fills it
    # in) for any duration-range country/category that doesn't set this.
    # Has no bearing on min_days/max_days validation, which is unaffected.
    default_duration_days: int | None = None


class TrTlConversionConfig(BaseModel):
    # Turkey-only: source_price_tl * rate + markup_rub, rounded to the
    # nearest RUB amount ending in 99 -- see
    # app.pricing.provider.available_periods, the only consumer, which
    # additionally requires country_code == "TR" before ever applying this
    # (not just "this period happens to have a price_tl") -- deliberately
    # named/scoped to Turkey specifically so a future country adding
    # price_tl by mistake can't silently start converting through this.
    # rate is Decimal (never float) -- config.yaml must quote it as a
    # string (e.g. "1.80") so no float precision is introduced before
    # Pydantic parses it.
    rate: Decimal
    markup_rub: int


class PricingSettings(BaseModel):
    # country -> vehicle_category_code -> periods
    periods_by_country_category: dict[str, dict[str, list[PeriodConfig]]]
    # country -> vehicle_category_code -> duration range, for EXACT DATE
    # RANGE products only (see DurationRangeConfig). A (country, category)
    # pair is either a fixed-period product (present in
    # periods_by_country_category) or an exact-duration one (present here),
    # never both -- app.web.checkout_routes checks this one first.
    duration_ranges_by_country_category: dict[str, dict[str, DurationRangeConfig]] = {}
    # None means Turkey has no TL-based periods configured yet (or the whole
    # mechanism is unused) -- see TrTlConversionConfig.
    tr_tl_conversion: TrTlConversionConfig | None = None


class CatalogSettings(BaseModel):
    # country -> allowed internal vehicle_category_code list (see
    # app.catalog.repository.list_categories's allowed_codes param, the only
    # consumer). A country absent from this mapping is UNRESTRICTED -- every
    # active category the catalog has is available, exactly today's
    # behaviour -- which is what keeps Georgia's category list untouched
    # (it's synced from tpl.ge, never hand-enumerated here) while still
    # letting a country be deliberately narrowed to an MVP subset (e.g. AM/TR
    # starting at just passenger_car -- see config/config.yaml).
    enabled_category_codes_by_country: dict[str, list[str]] = {}


class PaymentSettings(BaseModel):
    bank_name: str
    card_number: str
    card_holder: str
    qr_image_url: str | None = None
    # Direct link to pay via SBP/transfer (e.g. a bank's own payment link) --
    # an alternative to scanning the QR from the same device it's displayed
    # on. Optional and independent of qr_image_url: either, both, or neither
    # may be configured (see payment.html's rendering and app.web.routes'
    # get_payment). Never confirms payment by itself -- the customer still
    # has to come back and click "Я оплатил" (post_confirm_payment).
    transfer_url: str | None = None


class AppSettings(BaseModel):
    db_file: Path
    cookie_secure: bool = False
    secret_key: str = ""


class ContactSettings(BaseModel):
    max_url: str | None = None
    telegram_url: str | None = None
    vk_url: str | None = None


class OcrSettings(BaseModel):
    # Credential lives ONLY in the environment, never in config.yaml (see
    # app/ocr/provider.py) -- None means no real provider is configured, in
    # which case document recognition is reported as unavailable rather
    # than falling back to a hidden stub in production.
    openai_api_key: str | None = None
    vision_model: str = "gpt-5-mini"


class AdminSettings(BaseModel):
    # Credentials live ONLY in the environment, never in config.yaml (same
    # rule as OcrSettings.openai_api_key above). Either being None means
    # admin auth is NOT configured -- see app.deps.require_admin, which
    # must fail closed (401 on every request) rather than silently allowing
    # public access when these are unset.
    username: str | None = None
    password: str | None = None


class TplGeSettings(BaseModel):
    # Static, manually-captured FingerprintJS visitorId, reused verbatim as
    # the "visitorId" field TPL's real POST /api/policies expects -- NOT a
    # spoofed/generated fingerprint (that was explicitly ruled out during
    # discovery). Whether TPL's backend genuinely validates this value
    # server-side, versus merely logging it, was never confirmed -- reusing
    # a real value captured from an actual browser session is the only
    # non-fabricating option available, and it is exactly what discovery
    # testing itself already relied on (the same literal value was reused
    # across multiple real, successful calls). None means the integration
    # is not configured yet -- app.integrations.tpl_ge.service must refuse
    # to proceed with a clear operator-facing error rather than invent one,
    # same "None means off, fail closed" rule as OcrSettings/AdminSettings/
    # TelegramOperatorSettings above.
    static_visitor_id: str | None = None


class TelegramOperatorSettings(BaseModel):
    # Transport is Telethon (an authorized Telegram USER account), not the
    # Bot API -- no bot is created. api_id/api_hash/phone are the SAME
    # values already used by the separate ai-lead-radar project (same
    # Telegram account) -- see app.notifications.telegram module docstring
    # for why. session_path is auto-insurance's OWN dedicated .session
    # file, which must never be ai-lead-radar's reader_live/reader_sync/
    # reader_notifier/inviter sessions -- see
    # app.notifications.authorize_telegram_operator for the one-time login
    # that creates it. Credentials live ONLY in the environment, same rule
    # as OcrSettings/AdminSettings above. Any of api_id/api_hash/phone/
    # chat_id being None means the paid-order operator notification is
    # skipped (logged, never blocks/reverts the payment confirmation it's
    # reporting on) -- see app.notifications.telegram.
    api_id: int | None = None
    api_hash: str | None = None
    phone: str | None = None
    chat_id: int | str | None = None
    session_path: Path = Path("data/sessions/auto_insurance_operator")


class Settings(BaseModel):
    app: AppSettings
    pricing: PricingSettings
    catalog: CatalogSettings = CatalogSettings()
    payment: PaymentSettings
    contacts: ContactSettings = ContactSettings()
    ocr: OcrSettings = OcrSettings()
    admin: AdminSettings = AdminSettings()
    tpl_ge: TplGeSettings = TplGeSettings()
    telegram_operator: TelegramOperatorSettings = TelegramOperatorSettings()


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_telegram_chat_id(value: str | None) -> int | str | None:
    """Numeric chat/group ids (e.g. "-5535243432") become int -- Telethon
    needs the real int id for a chat it hasn't necessarily seen a message
    from recently. A "@username" (leading @ optional) is left as a string.
    Same convention as ai-lead-radar's own settings.py::_normalize_chat_id."""
    if not value:
        return None
    token = value.strip().lstrip("@")
    try:
        return int(token)
    except ValueError:
        return token


def load_settings(project_root: Path) -> Settings:
    project_root = Path(project_root)
    load_dotenv(project_root / ".env")

    # Override used only by tests (see tests/conftest.py + tests/fixtures/) so
    # they can exercise the full priced happy path without a real business
    # price ever having to live in the committed config/config.yaml.
    config_path = project_root / os.getenv("INSURANCE_CONFIG_FILE", "config/config.yaml")
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    pricing_raw = raw.get("pricing", {})
    try:
        periods_by_country_category = {
            country: {
                category_code: [PeriodConfig(**p) for p in periods]
                for category_code, periods in categories.items()
            }
            for country, categories in pricing_raw.items()
        }
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid pricing structure in {config_path}: {exc}") from exc

    duration_ranges_raw = raw.get("duration_ranges", {})
    try:
        duration_ranges_by_country_category = {
            country: {category_code: DurationRangeConfig(**d) for category_code, d in categories.items()}
            for country, categories in duration_ranges_raw.items()
        }
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid duration_ranges structure in {config_path}: {exc}") from exc

    # Sibling top-level key to pricing/duration_ranges, NOT nested inside
    # pricing: -- pricing_raw's own keys are all treated as country codes
    # (see periods_by_country_category above), so a conversion-config key
    # living there would be misparsed as a bogus country.
    tr_tl_conversion_raw = raw.get("tr_tl_conversion")
    try:
        tr_tl_conversion = TrTlConversionConfig(**tr_tl_conversion_raw) if tr_tl_conversion_raw else None
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid tr_tl_conversion structure in {config_path}: {exc}") from exc

    db_file = project_root / os.getenv("INSURANCE_DB_FILE", "data/insurance.db")

    catalog_raw = raw.get("catalog", {})
    enabled_category_codes_by_country = catalog_raw.get("enabled_category_codes_by_country", {}) or {}

    return Settings(
        app=AppSettings(
            db_file=db_file,
            cookie_secure=_parse_bool(os.getenv("COOKIE_SECURE")),
            secret_key=os.getenv("APP_SECRET_KEY", ""),
        ),
        pricing=PricingSettings(
            periods_by_country_category=periods_by_country_category,
            duration_ranges_by_country_category=duration_ranges_by_country_category,
            tr_tl_conversion=tr_tl_conversion,
        ),
        catalog=CatalogSettings(enabled_category_codes_by_country=enabled_category_codes_by_country),
        payment=PaymentSettings(
            bank_name=os.getenv("PAYMENT_BANK_NAME", "Bank"),
            card_number=os.getenv("PAYMENT_CARD_NUMBER", "0000 0000 0000 0000"),
            card_holder=os.getenv("PAYMENT_CARD_HOLDER", ""),
            qr_image_url=os.getenv("PAYMENT_QR_IMAGE_URL") or None,
            transfer_url=os.getenv("PAYMENT_TRANSFER_URL") or None,
        ),
        contacts=ContactSettings(
            max_url=os.getenv("CONTACT_MAX_URL") or None,
            telegram_url=os.getenv("CONTACT_TELEGRAM_URL") or None,
            vk_url=os.getenv("CONTACT_VK_URL") or None,
        ),
        ocr=OcrSettings(
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            vision_model=os.getenv("OCR_VISION_MODEL", "gpt-5-mini"),
        ),
        admin=AdminSettings(
            username=os.getenv("ADMIN_USERNAME") or None,
            password=os.getenv("ADMIN_PASSWORD") or None,
        ),
        tpl_ge=TplGeSettings(
            static_visitor_id=os.getenv("TPL_GE_STATIC_VISITOR_ID") or None,
        ),
        telegram_operator=TelegramOperatorSettings(
            api_id=int(os.getenv("TELEGRAM_API_ID")) if os.getenv("TELEGRAM_API_ID") else None,
            api_hash=os.getenv("TELEGRAM_API_HASH") or None,
            phone=os.getenv("TELEGRAM_PHONE") or None,
            chat_id=_parse_telegram_chat_id(os.getenv("TELEGRAM_OPERATOR_CHAT_ID")),
            session_path=project_root / os.getenv("TELEGRAM_OPERATOR_SESSION_PATH", "data/sessions/auto_insurance_operator"),
        ),
    )
