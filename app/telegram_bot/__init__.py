"""Customer-facing Telegram sales bot (aiogram 3, long polling).

Telegram-only concerns live here: update handling, keyboards, Russian
copy, conversation navigation. Every business rule (categories, periods,
prices, dates, draft/order storage) comes from app.checkout / app.pricing /
app.catalog / app.sessions -- exactly what the web checkout uses. Bot
identity/branding comes from a BotProfile (config.yaml telegram_bots.*),
never from code, so a second bot is configuration, not a fork.

Run: python -m app.telegram_bot
"""
