"""Policy period -> end_date calculation.

Confirmed by observing tpl.ge's own date picker (their production Georgia
product — same 15d/30d/90d/1y periods we're mirroring), not guessed: for a
start date of 15.08.2026 —

    15d -> 30.08.2026   (start + 15 days)
    30d -> 14.09.2026   (start + 30 days)
    90d -> 13.11.2026   (start + 90 days)
    1y  -> 15.08.2027   (start + 1 calendar year, same day/month)

So the rule is a plain "add the period" with no +-1 adjustment — no
inclusive/exclusive ambiguity to resolve. This is inferred from tpl.ge's UI
behaviour, not from Georgian legal text we've read ourselves, so it's still
worth a final legal/business confirmation before this is treated as
authoritative — but it is no longer an unverified guess.

Two things this observation cannot settle, both confirmed by direct
re-testing (screenshots, this review pass):

1. Month-end day-based periods (start=31.08.2026): 30d -> 30.09.2026,
   90d -> 29.11.2026, both re-confirmed live and consistent with the plain
   "start + N days" formula above — no special month-end handling exists or
   is needed.

2. The 1y leap-day case (start=29.02.2028) could NOT be re-verified live.
   tpl.ge's start-date picker only allows dates within roughly a 90-day
   window from today (confirmed empirically: with "today" = 12.08.2026, the
   last selectable day was 12.11.2026 — 93 enabled days including the
   start day, then every later day was disabled). The next 29 Feb (2028) is
   about 18 months out, structurally unreachable through their UI right
   now. So `test_1_year_clamps_leap_day_to_feb_28_in_non_leap_year` encodes
   our own defensible calendar-math choice (clamp to the last valid day of
   the target month, the same behaviour `dateutil.relativedelta` and most
   billing systems use), NOT an observed tpl.ge behaviour. Whether tpl.ge
   would clamp to 28 Feb or roll over to 1 Mar for that exact case is an
   open question we cannot answer from their UI/API alone — flag for legal
   confirmation rather than treating our clamp as authoritative.

Whether "end_date" itself is the last inclusive day of coverage or an
exclusive boundary is also not determinable from the UI/API alone — tpl.ge
never surfaces that distinction visually (it just shows a date), so this
is a legal/policy-wording question, not something scriptable evidence can
resolve.
"""

import calendar
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone

# Georgia has used a single fixed UTC+4 offset year-round since abolishing
# DST in 2005 -- a plain fixed-offset timezone is enough (and avoids an
# IANA tzdata dependency zoneinfo would need, which isn't bundled on
# Windows). This is the one place "today" is decided for start-date
# validation, so the business's calendar day never drifts from the
# server's/browser's own timezone.
GEORGIA_TZ = timezone(timedelta(hours=4), name="Georgia")


def today_in_georgia() -> date:
    return datetime.now(GEORGIA_TZ).date()


class UnknownPeriodCode(ValueError):
    pass


class DateRule(ABC):
    @abstractmethod
    def compute_end_date(self, start_date: date, period_code: str) -> date:
        raise NotImplementedError


def _add_calendar_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class GeorgiaDateRule(DateRule):
    """15d/30d/90d/1y, matching tpl.ge's Georgia product — see module docstring."""

    _DAY_PERIODS = {"15d": 15, "30d": 30, "90d": 90}
    _MONTH_PERIODS = {"1y": 12}

    def compute_end_date(self, start_date: date, period_code: str) -> date:
        if period_code in self._DAY_PERIODS:
            return start_date + timedelta(days=self._DAY_PERIODS[period_code])

        if period_code in self._MONTH_PERIODS:
            return _add_calendar_months(start_date, self._MONTH_PERIODS[period_code])

        raise UnknownPeriodCode(f"Unknown period_code: {period_code!r}")


class FixedDurationDateRule(DateRule):
    """Same "plain add the period, no +-1 adjustment" semantics as
    GeorgiaDateRule (see that class and the module docstring for the
    tpl.ge-observed evidence this mirrors) — parameterized by which period
    codes exist so a second fixed-period country (Turkey: 30d/45d/90d/180d/
    365d, all day-based, no month-based period like GE's legacy 1y) doesn't
    need its own hand-duplicated class. GeorgiaDateRule itself is left
    completely untouched by this addition — its own byte-for-byte behaviour
    and tests are not this class's concern."""

    def __init__(self, *, day_periods: dict[str, int] | None = None, month_periods: dict[str, int] | None = None):
        self._day_periods = dict(day_periods or {})
        self._month_periods = dict(month_periods or {})

    def compute_end_date(self, start_date: date, period_code: str) -> date:
        if period_code in self._day_periods:
            return start_date + timedelta(days=self._day_periods[period_code])

        if period_code in self._month_periods:
            return _add_calendar_months(start_date, self._month_periods[period_code])

        raise UnknownPeriodCode(f"Unknown period_code: {period_code!r}")
