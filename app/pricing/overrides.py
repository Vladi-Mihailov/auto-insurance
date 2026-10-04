"""Raw-SQL repository for insurance_price_overrides (see app.db for the
table's contract). Writers: /admin/prices only. Reader: app.pricing.provider."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# A plausibility ceiling for an admin-typed RUB price, not a business rule --
# it only stops an obvious typo (an extra digit or three) from going live.
MAX_PRICE_RUB = 1_000_000


@dataclass(frozen=True)
class PriceOverride:
    country_code: str
    vehicle_category_code: str
    period_code: str
    price_rub: int
    updated_at: str
    updated_by: str | None


def list_overrides(conn: sqlite3.Connection, country_code: str) -> list[PriceOverride]:
    rows = conn.execute(
        """SELECT country_code, vehicle_category_code, period_code, price_rub, updated_at, updated_by
           FROM insurance_price_overrides WHERE country_code = ?""",
        (country_code,),
    ).fetchall()
    return [PriceOverride(**dict(row)) for row in rows]


def upsert_override(
    conn: sqlite3.Connection,
    *,
    country_code: str,
    vehicle_category_code: str,
    period_code: str,
    price_rub: int,
    updated_by: str | None,
) -> None:
    if not (0 < price_rub <= MAX_PRICE_RUB):
        raise ValueError("price_rub out of range")
    conn.execute(
        """INSERT INTO insurance_price_overrides
               (country_code, vehicle_category_code, period_code, price_rub, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (country_code, vehicle_category_code, period_code) DO UPDATE SET
               price_rub = excluded.price_rub, updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
        (country_code, vehicle_category_code, period_code, price_rub, datetime.now(timezone.utc).isoformat(), updated_by),
    )


def delete_override(conn: sqlite3.Connection, *, country_code: str, vehicle_category_code: str, period_code: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM insurance_price_overrides WHERE country_code = ? AND vehicle_category_code = ? AND period_code = ?",
        (country_code, vehicle_category_code, period_code),
    )
    return cursor.rowcount > 0


def parse_price(raw: str) -> tuple[int | None, str | None]:
    """Whole rubles; spaces (incl. non-breaking) allowed as thousands separators.
    The one validation used by every price-writing surface (today: /admin/prices
    and the Telegram bot's "💰 Цены") -- never re-implement this elsewhere."""
    value = (raw or "").replace(" ", "").replace(" ", "")
    if not value:
        return None, "Укажите цену"
    if not value.isdigit():
        return None, "Только целое число рублей"
    price = int(value)
    if price <= 0:
        return None, "Цена должна быть больше нуля"
    if price > MAX_PRICE_RUB:
        return None, "Слишком большое значение"
    return price, None


def read_override_prices(db_file: Path, country_code: str, category_code: str) -> dict[str, int]:
    """period_code -> price_rub for one (country, category), read through a
    READ-ONLY connection so a missing DB file is never created as a side
    effect. Any failure to read (no DB yet, table not migrated yet) means
    "no overrides" -- the config price then applies, which is exactly the
    pre-override behaviour, never a guessed or zero price."""
    try:
        uri = Path(db_file).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except (sqlite3.Error, ValueError, OSError):
        return {}
    try:
        rows = conn.execute(
            """SELECT period_code, price_rub FROM insurance_price_overrides
               WHERE country_code = ? AND vehicle_category_code = ?""",
            (country_code, category_code),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {period_code: price_rub for period_code, price_rub in rows}
