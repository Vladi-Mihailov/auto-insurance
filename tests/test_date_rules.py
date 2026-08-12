"""GeorgiaDateRule — the 15d/30d/90d/1y formula is not a guess: it matches
tpl.ge's own date picker, observed directly (start=15.08.2026 across all
four periods). See app/dates/rules.py module docstring for the exact
observations these tests encode.

This supersedes the old 14d/1m/2m/3m ProvisionalGeorgiaDateRule — that
product model (and its date math) is intentionally not carried forward; see
the project notes on why (tpl.ge structure replaces it, not just relabels
it).
"""

from datetime import date

import pytest

from app.dates.rules import GeorgiaDateRule, UnknownPeriodCode

RULE = GeorgiaDateRule()


def test_15_days_matches_tplge_observed_behaviour():
    assert RULE.compute_end_date(date(2026, 8, 15), "15d") == date(2026, 8, 30)


def test_30_days_matches_tplge_observed_behaviour():
    assert RULE.compute_end_date(date(2026, 8, 15), "30d") == date(2026, 9, 14)


def test_90_days_matches_tplge_observed_behaviour():
    assert RULE.compute_end_date(date(2026, 8, 15), "90d") == date(2026, 11, 13)


def test_1_year_is_same_day_next_year_matches_tplge_observed_behaviour():
    assert RULE.compute_end_date(date(2026, 8, 15), "1y") == date(2027, 8, 15)


def test_1_year_clamps_leap_day_to_feb_28_in_non_leap_year():
    # 29 Feb 2028 (leap) + 1 year -> 2029 is not leap, clamp to 28 Feb.
    assert RULE.compute_end_date(date(2028, 2, 29), "1y") == date(2029, 2, 28)


def test_1_year_from_end_of_january_does_not_overflow_into_march():
    assert RULE.compute_end_date(date(2026, 1, 31), "1y") == date(2027, 1, 31)


def test_unknown_period_code_raises():
    with pytest.raises(UnknownPeriodCode):
        RULE.compute_end_date(date(2026, 1, 1), "14d")  # old code, no longer known to this rule
