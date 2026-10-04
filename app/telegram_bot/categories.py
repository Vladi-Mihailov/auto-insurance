"""Which vehicle categories a bot profile offers, and under what label --
ONE place both the customer checkout (steps.py) and the manager screens
(staff_prices.py, managers.py) read from, so a bot-specific product set
(e.g. a Turkey bot's "Truck / Camper") never needs its own copy of this
fallback logic.

A profile's own category_codes/category_labels (config.yaml telegram_bots.
<key>, see app.telegram_bot.profile.BotProfile) are OPTIONAL, additive, and
ENTIRELY SEPARATE from catalog.enabled_category_codes_by_country
(app.checkout.rules.allowed_category_codes) -- that stays the single source
of truth for the WEBSITE's country-level checkout availability and is never
read or modified here. A bot that sets category_codes gets EXACTLY that
list, in that order, independent of what the country-level list allows or
excludes; a bot that doesn't (Georgia today) keeps the existing
country-level behavior unchanged, byte for byte.
"""

import sqlite3

from app.catalog import repository as catalog_repo
from app.checkout import service as checkout_service
from app.settings import Settings
from app.telegram_bot import texts
from app.telegram_bot.profile import BotProfile


def offered_category_codes(conn: sqlite3.Connection, settings: Settings, profile: BotProfile) -> list[str]:
    """codes in display order. profile.category_codes, when set, is
    authoritative for this bot -- validated against the real (active)
    catalog so a typo never silently shows nothing or something that
    doesn't exist, but NOT filtered through allowed_category_codes: a bot's
    own product choice is independent of the country-level website list."""
    if profile.category_codes is not None:
        catalog_codes = {c.code for c in catalog_repo.list_categories(conn)}
        return [code for code in profile.category_codes if code in catalog_codes]
    return [c.code for c in checkout_service.list_offered_categories(conn, settings, profile.country_code)]


def category_label(conn: sqlite3.Connection, profile: BotProfile, code: str) -> str:
    """profile.category_labels first (this bot's own override), then the
    global customer-facing label (texts.CATEGORY_LABELS), then the synced
    catalog name -- same three-step fallback every caller used to implement
    on its own (app.telegram_bot.steps/staff_prices/managers)."""
    if profile.category_labels and code in profile.category_labels:
        return profile.category_labels[code]
    if code in texts.CATEGORY_LABELS:
        return texts.CATEGORY_LABELS[code]
    category = catalog_repo.get_category_by_code(conn, code)
    return category.name if category else code
