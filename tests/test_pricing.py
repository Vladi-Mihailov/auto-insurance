from pathlib import Path

from app.pricing.provider import available_periods, get_period
from app.settings import AppSettings, PaymentSettings, PeriodConfig, PricingSettings, Settings


def _settings():
    return Settings(
        app=AppSettings(db_file=Path("unused.db")),
        pricing=PricingSettings(
            periods_by_country_category={
                "GE": {
                    "passenger_car": [
                        PeriodConfig(code="15d", label="15 дней", price_rub=1500),
                        PeriodConfig(code="30d", label="30 дней", price_rub=None),  # not configured yet
                    ]
                }
            }
        ),
        payment=PaymentSettings(bank_name="Bank", card_number="0000", card_holder="X"),
    )


def test_available_periods_reads_from_config():
    periods = available_periods(_settings(), "GE", "passenger_car")
    assert [p.code for p in periods] == ["15d", "30d"]
    assert periods[0].price_rub == 1500
    assert periods[0].price_minor == 150000


def test_available_periods_unpriced_period_is_not_priced():
    periods = available_periods(_settings(), "GE", "passenger_car")
    unpriced = periods[1]
    assert unpriced.is_priced is False
    assert unpriced.price_minor is None


def test_available_periods_unknown_country_is_empty():
    assert available_periods(_settings(), "AM", "passenger_car") == []


def test_available_periods_unknown_category_is_empty():
    assert available_periods(_settings(), "GE", "motorcycle") == []


def test_get_period_found_and_missing():
    assert get_period(_settings(), "GE", "passenger_car", "15d").price_rub == 1500
    assert get_period(_settings(), "GE", "passenger_car", "99y") is None
