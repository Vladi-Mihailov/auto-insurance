"""Pricing lookup, sourced from config.yaml — never hardcoded in templates/handlers.

Country + vehicle category select which period/price list applies. A period
can exist (code/label known — these come from the product definition) with
price_rub still unset — that's a real, expected state ("not priced yet"),
not a bug; callers must handle it (see PeriodOption.is_priced) rather than
treating None as 0 or falling back to a guess.
"""

from dataclasses import dataclass

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
