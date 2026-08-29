"""Pricing lookup, sourced from config.yaml — never hardcoded in templates/handlers.

Country + vehicle category select which period/price list applies. A period
can exist (code/label known — these come from the product definition) with
price_rub still unset — that's a real, expected state ("not priced yet"),
not a bug; callers must handle it (see PeriodOption.is_priced) rather than
treating None as 0 or falling back to a guess.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from app.settings import Settings


@dataclass(frozen=True)
class PeriodOption:
    code: str
    label: str
    price_rub: int | None

    @property
    def is_priced(self) -> bool:
        return self.price_rub is not None

    @property
    def price_minor(self) -> int | None:
        return self.price_rub * 100 if self.price_rub is not None else None


def available_periods(settings: Settings, country_code: str, category_code: str) -> list[PeriodOption]:
    categories = settings.pricing.periods_by_country_category.get(country_code, {})
    periods = categories.get(category_code, [])
    return [PeriodOption(code=p.code, label=p.label, price_rub=p.price_rub) for p in periods]


def get_period(settings: Settings, country_code: str, category_code: str, period_code: str) -> PeriodOption | None:
    for period in available_periods(settings, country_code, category_code):
        if period.code == period_code:
            return period
    return None


@dataclass(frozen=True)
class DurationRange:
    min_days: int
    max_days: int


def get_duration_range(settings: Settings, country_code: str, category_code: str) -> DurationRange | None:
    """None means this (country, category) is a FIXED-period product (or
    simply not configured at all) -- see available_periods/get_period for
    that case. Non-None means the opposite: an EXACT DATE RANGE product
    (currently only AM passenger_car) where the customer picks start_date/
    end_date directly rather than choosing a period code -- see
    app.web.checkout_routes._parse_duration_range_dates, the only place
    that validates against these bounds."""
    config = settings.pricing.duration_ranges_by_country_category.get(country_code, {}).get(category_code)
    if config is None:
        return None
    return DurationRange(min_days=config.min_days, max_days=config.max_days)


def _round_half_up_to_rub(value: Fraction) -> int:
    """Fraction -> whole RUB via ROUND_HALF_UP (ties away from zero).

    Uses Fraction for the arithmetic leading up to this so the line's
    non-terminating slope (e.g. 800/15) never loses precision; Decimal is
    only introduced here, at the single point where rounding actually
    happens, via an exact numerator/denominator division (default Decimal
    context precision is 28 significant digits -- vastly more than these
    small RUB amounts ever need)."""
    return int((Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def resolve_duration_price(
    settings: Settings, country_code: str, category_code: str, duration_days: int
) -> int | None:
    """RUB-minor price for an EXACT DATE RANGE product (see DurationRange
    above), computed from two reference points as a straight RUB-per-day
    line and then a percent discount -- both defined in
    duration_ranges.<country>.<category>.pricing (see LinearDurationPricingConfig).
    None means this (country, category) has a duration range configured but
    no pricing yet -- callers must treat that exactly like "not priced",
    same as PeriodOption.is_priced elsewhere; it is not this function's job
    to reject a duration_days outside min_days/max_days (that's
    app.web.checkout_routes._parse_duration_range_dates, which always runs
    first) -- the line intentionally extrapolates across the whole
    configured range, it does not stop being valid outside the two
    reference points.

    Order of operations (fixed, not customizable): linear base price ->
    round to whole RUB -> apply discount_percent -> round to whole RUB again
    -> convert to minor units. Every step uses exact rational (Fraction) or
    Decimal arithmetic, never float.
    """
    config = settings.pricing.duration_ranges_by_country_category.get(country_code, {}).get(category_code)
    if config is None or config.pricing is None:
        return None
    pricing = config.pricing

    slope = Fraction(pricing.reference_price_rub_2 - pricing.reference_price_rub_1, pricing.reference_days_2 - pricing.reference_days_1)
    base_price_exact = Fraction(pricing.reference_price_rub_1) + Fraction(duration_days - pricing.reference_days_1) * slope
    base_price_rub = _round_half_up_to_rub(base_price_exact)

    if pricing.discount_percent:
        discounted_exact = Fraction(base_price_rub) * Fraction(100 - pricing.discount_percent, 100)
        final_rub = _round_half_up_to_rub(discounted_exact)
    else:
        final_rub = base_price_rub

    return final_rub * 100
