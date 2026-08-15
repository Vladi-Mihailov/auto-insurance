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


class Settings(BaseModel):
    app: AppSettings
    pricing: PricingSettings
    payment: PaymentSettings
    contacts: ContactSettings = ContactSettings()
    ocr: OcrSettings = OcrSettings()
    admin: AdminSettings = AdminSettings()


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


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
    )
