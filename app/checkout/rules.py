"""Transport-independent checkout rules: which countries/categories/periods
exist, how start/end dates are validated and computed, and which optional
fields a (country, category) requires.

Moved verbatim out of app.web.checkout_routes (which re-exports every name
under its old private alias) so the web checkout and the Telegram bot apply
exactly the same rules -- never a second copy. Pricing itself stays in
app.pricing.provider and date math in app.dates.rules; this module only
composes them.
"""

from datetime import date

from app.dates.rules import DateRule, FixedDurationDateRule, GeorgiaDateRule
from app.pricing.provider import DurationRange, get_duration_range

DATE_RULE = GeorgiaDateRule()

# The only checkout-level notion of "which countries exist" right now (step 1
# of the multi-country rollout -- see the GE/AM/TR gap-analysis report this
# implements). Georgia is FIXED-period (DATE_RULE above); Turkey is also
# fixed-period but with its own period set/prices-not-yet-set (see
# _FIXED_DURATION_DATE_RULES/config.yaml's pricing.TR); Armenia is EXACT
# DATE RANGE (see duration_range_for/parse_duration_range_dates) -- neither
# AM nor TR has a real customer price yet (out of scope for this step, see
# the gap-analysis report's PricingProvider design), so neither can reach
# order creation through the real UI flow today. That's deliberate, not a
# bug -- see app.web.checkout_routes.post_policyholder's price guard.
SUPPORTED_COUNTRY_CODES = ("GE", "AM", "TR")
DEFAULT_COUNTRY_CODE = "GE"

# FIXED-period date rule per country -- GE's own untouched GeorgiaDateRule,
# plus Turkey's own period set via the generic FixedDurationDateRule (see
# app.dates.rules). A country absent here (Armenia) is never fixed-period at
# all right now -- callers must check duration_range_for first (see
# fixed_duration_date_rule's own docstring).
_FIXED_DURATION_DATE_RULES: dict[str, DateRule] = {
    "GE": DATE_RULE,
    "TR": FixedDurationDateRule(day_periods={"30d": 30, "45d": 45, "90d": 90, "180d": 180, "365d": 365}),
}


def draft_country_code(draft: dict | None) -> str:
    """The country chosen at /start (see app.web.routes.start), carried
    through the whole pre-order draft. Falls back to DEFAULT_COUNTRY_CODE
    for a draft that predates this key (an in-flight session from before
    this change, or any direct/bookmarked entry into the wizard that
    skipped /start entirely) -- this deep into the wizard a missing/unknown
    country is never a user-facing error, just "assume Georgia", exactly
    the implicit behaviour every existing GE checkout already relied on."""
    country_code = (draft or {}).get("country_code")
    return country_code if country_code in SUPPORTED_COUNTRY_CODES else DEFAULT_COUNTRY_CODE


def allowed_category_codes(settings, country_code: str) -> list[str] | None:
    """The single source of truth for "which internal vehicle_category_code
    values does this country's checkout offer" (see
    app.catalog.repository.list_categories's allowed_codes param, the only
    consumer of this return value). None means unrestricted -- a country
    absent from config.yaml's catalog.enabled_category_codes_by_country
    (Georgia today) sees every active category exactly as before this
    change; AM/TR are explicitly narrowed there instead of here, so enabling
    more categories later is a config edit, never a code change."""
    return settings.catalog.enabled_category_codes_by_country.get(country_code)


# Single source of truth for "which country requires which of the new
# AM/TR-only fields" (see the gap-analysis report's field matrix) -- every
# route that shows, validates, or persists these fields goes through these
# three, never a hardcoded country check duplicated per route/template.

# Categories with no engine at all -- engine_power is never shown/required
# for these regardless of country, even where the country would otherwise
# require it (AM). Currently just trailer; a set (not a single hardcoded
# check) so adding another engine-less category later is a one-line change
# here, not a new branch.
CATEGORIES_WITHOUT_ENGINE = {"trailer"}


def requires_engine_power(country_code: str, category_code: str) -> bool:
    # TR no longer requires this for ordinary OSAGO purchase (business
    # decision, 2026-09-06): the source site (strahovka-turkiye.com) only
    # asks for engine/motor info for a separate, unrelated "Turkish plates
    # under customs deposit" service, not for buying the policy itself.
    if category_code in CATEGORIES_WITHOUT_ENGINE:
        return False
    return country_code == "AM"


def requires_model_year(country_code: str) -> bool:
    return country_code == "TR"


def requires_date_of_birth(country_code: str) -> bool:
    return country_code == "TR"


def duration_range_for(settings, country_code: str, category_code: str) -> DurationRange | None:
    """None means (country, category) is a FIXED-period product (GE/TR
    today) -- non-None means EXACT DATE RANGE (AM's passenger_car) where the
    customer picks start_date/end_date directly instead of a period code.
    Thin pass-through to app.pricing.provider.get_duration_range, kept as
    its own helper so every caller in this module goes through the exact
    same check rather than reaching into settings.pricing directly."""
    return get_duration_range(settings, country_code, category_code)


def fixed_duration_date_rule(country_code: str) -> DateRule:
    """Only ever called after confirming duration_range_for(...) is None for
    this (country, category) -- Armenia has no entry here at all (it isn't
    a fixed-period country), so an unrecognized/AM country_code falls back
    to Georgia's own rule, same "assume Georgia" default used everywhere
    else in this module. Callers must not rely on that fallback ever firing
    for AM in practice -- it's a safety net, not a real code path."""
    return _FIXED_DURATION_DATE_RULES.get(country_code, DATE_RULE)


def category_period_step_completed(settings, draft: dict | None) -> bool:
    """True once /category-period's OWN required data is present: always a
    category, plus -- for a FIXED-period (country, category) -- a
    period_code too. An EXACT DATE RANGE product (AM) has nothing further
    to pick on /category-period at all (see post_category_period): its
    period IS the start_date/end_date chosen on the next step, so a bare
    category is already "done" here."""
    if not draft or draft.get("vehicle_category_code") is None:
        return False
    country_code = draft_country_code(draft)
    if duration_range_for(settings, country_code, draft["vehicle_category_code"]) is not None:
        return True
    return draft.get("period_code") is not None


def parse_duration_range_dates(
    start_raw: str, end_raw: str, *, today: date, duration_range: DurationRange
) -> tuple[date | None, date | None, str | None]:
    """AM-style EXACT DATE RANGE validation: both dates are direct user
    input (see the module-level comment on _FIXED_DURATION_DATE_RULES) --
    there is no period_code to compute end_date FROM, so this is pure
    validation, not date-math. duration_days uses the exact same "end -
    start, in calendar days" definition GeorgiaDateRule's own period codes
    already imply (see that class's docstring: "15d" means start_date + 15
    days exactly, i.e. end_date - start_date == 15) -- so "10 days" here
    means end_date is start_date + 10 days: duration_range.min_days <=
    (end_date - start_date).days <= duration_range.max_days, inclusive on
    both ends. Returns (start_date, end_date, error) -- the first two are
    None whenever error is not None, same shape as
    parse_and_validate_start_date."""
    parsed_start, error = parse_and_validate_start_date(start_raw, today)
    if error:
        return None, None, error

    try:
        parsed_end = date.fromisoformat(end_raw)
    except ValueError:
        return None, None, "Некорректная дата окончания"

    duration_days = (parsed_end - parsed_start).days
    if duration_days < duration_range.min_days:
        return None, None, f"Минимальный срок страхования — {duration_range.min_days} дней"
    if duration_days > duration_range.max_days:
        return None, None, f"Максимальный срок страхования — {duration_range.max_days} дней"
    return parsed_start, parsed_end, None


def require_draft_keys(draft: dict | None, keys: tuple[str, ...]) -> bool:
    if not draft:
        return False
    return all(draft.get(key) is not None for key in keys)


def parse_and_validate_start_date(raw: str, today: date) -> tuple[date | None, str | None]:
    """Server-side source of truth for "start date can't be in the past" --
    the HTML min= attribute (see date_step.html) is a UX nicety only and
    must never be trusted alone, since a direct POST bypasses it entirely."""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None, "Некорректная дата"
    return validate_start_date(parsed, today)


def validate_start_date(start_date: date, today: date) -> tuple[date | None, str | None]:
    """The "not before today (Georgia time)" rule itself, for a caller that
    has already parsed the date from its own input format (the Telegram
    bot's DD.MM.YYYY / quick buttons) -- parse_and_validate_start_date
    above is just ISO parsing in front of this same check. No upper bound:
    none is confirmed for the product yet (see app.dates.rules)."""
    if start_date < today:
        return None, "Дата начала не может быть раньше сегодняшнего дня"
    return start_date, None
