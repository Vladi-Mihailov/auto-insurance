"""Retail-price administration -- the ONE implementation of the price-editing
rules, shared by the website's /admin/prices (app.web.admin_prices_routes)
and the Telegram bot's "💰 Цены" (app.telegram_bot.staff_prices). Neither UI
writes insurance_price_overrides itself.

Model (unchanged): config/config.yaml defines which (country, category,
period) cells exist and their BASE price (app.pricing.provider.
config_periods); an insurance_price_overrides row replaces that price for
exactly one cell (app.pricing.overrides). The effective price every checkout
uses is still app.pricing.provider.available_periods -- this module only
reads the same two sources to describe a cell and writes overrides.

Rules enforced here, for every caller:
- only a cell config.yaml defines can be written (UnknownCell otherwise) --
  a config period with a null base price may receive an override;
- every write carries the value the editor last SAW (expected_*) and is
  refused with StalePrice if the cell changed since -- checked and written
  inside one write transaction, so two managers never silently overwrite
  each other;
- saving the base price removes the override instead of storing a copy;
- every change appends one insurance_price_override_history row.

Existing orders are never touched: each order keeps the
price_customer_minor snapshotted at creation.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from app.catalog import repository as catalog_repo
from app.checkout import service as checkout_service
from app.pricing import overrides as price_overrides
from app.pricing.provider import config_periods
from app.settings import Settings

logger = logging.getLogger(__name__)

SOURCE_WEB = "web"
SOURCE_TELEGRAM = "telegram"
CHANNEL_WEB = "web"
CHANNEL_BOT = "bot"


class PriceAdminError(Exception):
    pass


class UnknownCell(PriceAdminError):
    """No such (country, category, period) in config.yaml."""


class StalePrice(PriceAdminError):
    """The cell changed after the editor loaded it; nothing was written."""

    def __init__(self, cell: "PriceCell"):
        super().__init__("price changed since it was shown")
        self.cell = cell


@dataclass(frozen=True)
class PriceCell:
    country_code: str
    category_code: str
    period_code: str
    period_label: str
    base_rub: int | None
    override_rub: int | None
    updated_at: str | None
    updated_by: str | None

    @property
    def has_override(self) -> bool:
        return self.override_rub is not None

    @property
    def effective_rub(self) -> int | None:
        # Same rule as app.pricing.provider.available_periods.
        return self.override_rub if self.override_rub is not None else self.base_rub

    @property
    def is_priced(self) -> bool:
        return self.effective_rub is not None


@dataclass(frozen=True)
class PriceRow:
    category_code: str
    label: str
    channels: tuple[str, ...]  # CHANNEL_WEB and/or CHANNEL_BOT
    cells: dict[str, PriceCell]  # period_code -> cell


@dataclass(frozen=True)
class PriceMatrix:
    country_code: str
    periods: list[tuple[str, str]]  # (code, label), in config order
    rows: list[PriceRow]


@dataclass(frozen=True)
class PriceChange:
    cell: PriceCell  # as it was BEFORE the change
    action: str
    old_rub: int | None
    new_rub: int | None


# ------------------------------------------------------------------- read


def get_cell(
    conn: sqlite3.Connection, settings: Settings, country_code: str, category_code: str, period_code: str
) -> PriceCell | None:
    """None -- config.yaml has no such cell."""
    period = next((p for p in config_periods(settings, country_code, category_code) if p.code == period_code), None)
    if period is None:
        return None
    row = conn.execute(
        """SELECT price_rub, updated_at, updated_by FROM insurance_price_overrides
           WHERE country_code = ? AND vehicle_category_code = ? AND period_code = ?""",
        (country_code, category_code, period_code),
    ).fetchone()
    return PriceCell(
        country_code=country_code,
        category_code=category_code,
        period_code=period.code,
        period_label=period.label,
        base_rub=period.price_rub,
        override_rub=row["price_rub"] if row else None,
        updated_at=row["updated_at"] if row else None,
        updated_by=row["updated_by"] if row else None,
    )


def _bot_profiles(settings: Settings, country_code: str):
    return [p for p in settings.telegram_bot_profiles.values() if p.country_code == country_code]


def category_channels(conn: sqlite3.Connection, settings: Settings, country_code: str) -> dict[str, tuple[str, ...]]:
    """category_code -> where it is sold, in display order: the website's
    country list first, then categories only a bot of this country sells
    (e.g. TR's "🚛 Truck / Camper"). A bot without its own category_codes
    sells the website list (app.telegram_bot.categories)."""
    web_codes = [c.code for c in checkout_service.list_offered_categories(conn, settings, country_code)]
    catalog_codes = {c.code for c in catalog_repo.list_categories(conn)}
    channels: dict[str, list[str]] = {code: [CHANNEL_WEB] for code in web_codes}
    for profile in _bot_profiles(settings, country_code):
        bot_codes = (
            [c for c in profile.category_codes if c in catalog_codes] if profile.category_codes is not None else web_codes
        )
        for code in bot_codes:
            entry = channels.setdefault(code, [])
            if CHANNEL_BOT not in entry:
                entry.append(CHANNEL_BOT)
    return {code: tuple(chs) for code, chs in channels.items()}


def _category_label(conn: sqlite3.Connection, settings: Settings, country_code: str, code: str, channels) -> str:
    if CHANNEL_WEB not in channels:
        for profile in _bot_profiles(settings, country_code):
            if profile.category_labels and code in profile.category_labels:
                return profile.category_labels[code]
    category = catalog_repo.get_category_by_code(conn, code)
    return category.name if category else code


def price_matrix(conn: sqlite3.Connection, settings: Settings, country_code: str) -> PriceMatrix:
    """Every category this country sells (website and/or bot) that
    config.yaml prices as fixed periods, with its own periods only -- a
    category simply has no cell for a period config doesn't define."""
    periods: dict[str, str] = {}
    rows = []
    for code, channels in category_channels(conn, settings, country_code).items():
        config = config_periods(settings, country_code, code)
        if not config:
            continue
        cells = {}
        for period in config:
            periods.setdefault(period.code, period.label)
            cells[period.code] = get_cell(conn, settings, country_code, code, period.code)
        rows.append(
            PriceRow(
                category_code=code,
                label=_category_label(conn, settings, country_code, code, channels),
                channels=channels,
                cells=cells,
            )
        )
    return PriceMatrix(country_code=country_code, periods=list(periods.items()), rows=rows)


# ------------------------------------------------------------------ write


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE takes SQLite's write lock BEFORE the stale check, so
    nothing can change the cell between the check and the write. A caller
    already inside a transaction keeps it (and already holds the lock,
    since Python's sqlite3 only opens one implicitly for a write)."""
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if owned:
            conn.rollback()
        raise
    conn.commit()


def _record_history(conn, cell: PriceCell, *, action: str, old_rub, new_rub, actor: str | None, source: str) -> None:
    conn.execute(
        """INSERT INTO insurance_price_override_history
               (country_code, vehicle_category_code, period_code, action, old_price_rub, new_price_rub,
                actor, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            cell.country_code, cell.category_code, cell.period_code, action, old_rub, new_rub,
            actor, source, datetime.now(timezone.utc).isoformat(),
        ),
    )


def set_price(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    country_code: str,
    category_code: str,
    period_code: str,
    expected_rub: int | None,
    new_rub: int,
    actor: str | None,
    source: str,
) -> PriceChange | None:
    """expected_rub: the EFFECTIVE price the editor saw (None = it was not
    priced). Returns None when new_rub already is the effective price
    (nothing written). Raises UnknownCell / StalePrice / ValueError (price
    out of range -- callers validate input with
    app.pricing.overrides.parse_price first)."""
    if not (0 < new_rub <= price_overrides.MAX_PRICE_RUB):
        raise ValueError("price_rub out of range")
    with _write_transaction(conn):
        cell = get_cell(conn, settings, country_code, category_code, period_code)
        if cell is None:
            raise UnknownCell(f"{country_code}/{category_code}/{period_code}")
        if cell.effective_rub != expected_rub:
            raise StalePrice(cell)
        if new_rub == cell.effective_rub:
            return None
        if new_rub == cell.base_rub:
            price_overrides.delete_override(
                conn, country_code=country_code, vehicle_category_code=category_code, period_code=period_code
            )
        else:
            price_overrides.upsert_override(
                conn,
                country_code=country_code,
                vehicle_category_code=category_code,
                period_code=period_code,
                price_rub=new_rub,
                updated_by=actor,
            )
        _record_history(conn, cell, action="set", old_rub=cell.effective_rub, new_rub=new_rub, actor=actor, source=source)
    logger.info(
        "Retail price changed: %s/%s/%s %s -> %s RUB by %s %r%s",
        country_code, category_code, period_code, cell.effective_rub, new_rub, source, actor,
        " (equals config base, override removed)" if new_rub == cell.base_rub else "",
    )
    return PriceChange(cell=cell, action="set", old_rub=cell.effective_rub, new_rub=new_rub)


def reset_price(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    country_code: str,
    category_code: str,
    period_code: str,
    expected_override_rub: int | None,
    actor: str | None,
    source: str,
) -> PriceChange | None:
    """Remove the cell's override so the config.yaml base applies again.
    expected_override_rub: the override the editor saw. Returns None when
    there is no override (nothing to reset). Raises UnknownCell /
    StalePrice."""
    with _write_transaction(conn):
        cell = get_cell(conn, settings, country_code, category_code, period_code)
        if cell is None:
            raise UnknownCell(f"{country_code}/{category_code}/{period_code}")
        if cell.override_rub != expected_override_rub:
            raise StalePrice(cell)
        if not cell.has_override:
            return None
        price_overrides.delete_override(
            conn, country_code=country_code, vehicle_category_code=category_code, period_code=period_code
        )
        _record_history(
            conn, cell, action="reset", old_rub=cell.override_rub, new_rub=cell.base_rub, actor=actor, source=source
        )
    logger.info(
        "Retail price override removed: %s/%s/%s %s -> %s RUB (config base) by %s %r",
        country_code, category_code, period_code, cell.override_rub, cell.base_rub, source, actor,
    )
    return PriceChange(cell=cell, action="reset", old_rub=cell.override_rub, new_rub=cell.base_rub)


def history(conn: sqlite3.Connection, country_code: str, *, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM insurance_price_override_history WHERE country_code = ?
           ORDER BY id DESC LIMIT ?""",
        (country_code, limit),
    ).fetchall()
