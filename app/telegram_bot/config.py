"""Runtime configuration for the bot process -- validated once at startup,
FAILS CLOSED: the bot refuses to start unless a token, a known bot profile
and at least one manager id are configured. Error messages name the
missing/invalid VARIABLE, never its value (the token especially)."""

import dataclasses
import re
from dataclasses import dataclass

from pydantic import SecretStr

from app.checkout.rules import SUPPORTED_COUNTRY_CODES
from app.checkout.service import validate_policyholder_field
from app.settings import Settings
from app.telegram_bot.profile import BotProfile

# Telegram bot token shape: "<numeric bot id>:<35-ish url-safe chars>".
# Checked only to catch an obviously wrong paste early -- Telegram itself is
# the real authority.
_TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,64}$")
_BOT_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class BotConfigError(Exception):
    pass


@dataclass(frozen=True)
class BotConfig:
    profile: BotProfile
    token: SecretStr
    # The env-configured managers: bootstrapped into telegram_bot_staff at
    # start (app.telegram_bot.staff.bootstrap) -- authorization itself
    # always reads that table.
    manager_ids: frozenset[int]
    owner_id: int | None = None

    def __repr__(self) -> str:  # never render the token, even masked
        return f"BotConfig(bot_key={self.profile.bot_key!r}, managers={len(self.manager_ids)})"


def parse_manager_ids(raw: str | None) -> frozenset[int]:
    """Comma/space-separated positive numeric Telegram user ids."""
    if not raw or not raw.strip():
        raise BotConfigError("TELEGRAM_BOT_MANAGER_IDS is not set -- at least one manager Telegram user id is required")
    ids = set()
    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue
        if not token.isdigit() or int(token) <= 0:
            raise BotConfigError("TELEGRAM_BOT_MANAGER_IDS must be comma-separated numeric Telegram user ids")
        ids.add(int(token))
    if not ids:
        raise BotConfigError("TELEGRAM_BOT_MANAGER_IDS is not set -- at least one manager Telegram user id is required")
    return frozenset(ids)


def parse_owner_id(raw: str | None) -> int | None:
    if not raw or not raw.strip():
        return None
    if not raw.strip().isdigit() or int(raw.strip()) <= 0:
        raise BotConfigError("TELEGRAM_BOT_OWNER_ID must be one numeric Telegram user id")
    return int(raw.strip())


def load_bot_config(settings: Settings) -> BotConfig:
    bot_settings = settings.telegram_bot

    if bot_settings.token is None:
        raise BotConfigError("TELEGRAM_BOT_TOKEN is not set")
    if not _TOKEN_RE.match(bot_settings.token.get_secret_value()):
        raise BotConfigError("TELEGRAM_BOT_TOKEN does not look like a Telegram bot token (value not shown)")

    bot_key = bot_settings.bot_key
    if not bot_key:
        raise BotConfigError("TELEGRAM_BOT_KEY is not set")
    if not _BOT_KEY_RE.match(bot_key):
        raise BotConfigError("TELEGRAM_BOT_KEY must match [a-z0-9_]{1,32}")
    profile_config = settings.telegram_bot_profiles.get(bot_key)
    if profile_config is None:
        raise BotConfigError(f"No telegram_bots.{bot_key} profile in config.yaml for TELEGRAM_BOT_KEY")
    if profile_config.country_code not in SUPPORTED_COUNTRY_CODES:
        raise BotConfigError(f"telegram_bots.{bot_key}.country_code is not a supported country")

    profile = BotProfile.from_config(bot_key, profile_config)
    # Fixed contacts go onto real orders (and to tpl.ge) -- they must pass the
    # same validators a typed value would; stored in their normalized form.
    normalized = {}
    for field, value in profile.fixed_contacts().items():
        clean, error = validate_policyholder_field(field, value)
        if error:
            raise BotConfigError(f"telegram_bots.{bot_key}: invalid {'customer_email' if field == 'contact_email' else 'customer_phone'}")
        normalized[field] = clean
    profile = dataclasses.replace(
        profile,
        customer_email=normalized.get("contact_email", profile.customer_email),
        customer_phone=normalized.get("contact_phone", profile.customer_phone),
    )

    return BotConfig(
        profile=profile,
        token=bot_settings.token,
        manager_ids=parse_manager_ids(bot_settings.manager_ids_raw),
        owner_id=parse_owner_id(bot_settings.owner_id_raw),
    )
