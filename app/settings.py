"""Configuration loading: config/config.yaml (non-secret structure) + .env (secrets).

Mirrors the split used elsewhere for this kind of project: prices/periods
are checked into config.yaml, while payment requisites, cookie behaviour
and DB path overrides come from environment variables so they never end up
committed to git.
"""

import os
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


class PricingSettings(BaseModel):
    # country -> vehicle_category_code -> periods
    periods_by_country_category: dict[str, dict[str, list[PeriodConfig]]]


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
    payment: PaymentSettings
    contacts: ContactSettings = ContactSettings()
    ocr: OcrSettings = OcrSettings()
    admin: AdminSettings = AdminSettings()
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

    db_file = project_root / os.getenv("INSURANCE_DB_FILE", "data/insurance.db")

    return Settings(
        app=AppSettings(
            db_file=db_file,
            cookie_secure=_parse_bool(os.getenv("COOKIE_SECURE")),
            secret_key=os.getenv("APP_SECRET_KEY", ""),
        ),
        pricing=PricingSettings(periods_by_country_category=periods_by_country_category),
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
        telegram_operator=TelegramOperatorSettings(
            api_id=int(os.getenv("TELEGRAM_API_ID")) if os.getenv("TELEGRAM_API_ID") else None,
            api_hash=os.getenv("TELEGRAM_API_HASH") or None,
            phone=os.getenv("TELEGRAM_PHONE") or None,
            chat_id=_parse_telegram_chat_id(os.getenv("TELEGRAM_OPERATOR_CHAT_ID")),
            session_path=project_root / os.getenv("TELEGRAM_OPERATOR_SESSION_PATH", "data/sessions/auto_insurance_operator"),
        ),
    )
