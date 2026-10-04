"""Bot identity/branding, loaded from config.yaml telegram_bots.<bot_key>.

Handlers read everything bot-specific (country, intro copy) from here --
no handler or domain module names a concrete bot."""

from dataclasses import dataclass

from app.settings import TelegramBotProfileConfig


@dataclass(frozen=True)
class BotProfile:
    bot_key: str
    country_code: str
    intro_title: str
    intro_text: str
    username: str | None = None
    # Fixed policyholder contacts for every order this bot takes (see
    # TelegramBotProfileConfig) -- None means "ask the customer".
    customer_email: str | None = None
    customer_phone: str | None = None

    def fixed_contacts(self) -> dict:
        """Draft/order fields (insurance_orders column names) this bot fills
        itself instead of asking the customer."""
        values = {"contact_email": self.customer_email, "contact_phone": self.customer_phone}
        return {field: value for field, value in values.items() if value}

    @classmethod
    def from_config(cls, bot_key: str, config: TelegramBotProfileConfig) -> "BotProfile":
        return cls(
            bot_key=bot_key,
            country_code=config.country_code,
            intro_title=config.intro_title,
            intro_text=config.intro_text,
            username=config.username,
            customer_email=config.customer_email,
            customer_phone=config.customer_phone,
        )
