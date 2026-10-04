"""Admin retail-price editor (/admin/prices).

DB overrides on top of config/config.yaml (see app.pricing.overrides),
read by app.pricing.provider for the web checkout AND the Telegram bot
alike -- there is exactly one effective price per (country, category,
period). Existing orders are never touched: each order keeps the
price_customer_minor it was created with.

Same protection as every other /admin/* route: router-wide require_admin
(HTTP Basic, fails closed when admin credentials aren't configured).
"""

import logging
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.checkout import service as checkout_service
from app.deps import current_admin_username, get_db, get_settings, require_admin
from app.pricing import overrides as price_overrides
from app.pricing.overrides import parse_price
from app.pricing.provider import available_periods, config_periods
from app.web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

# The only product with editable retail prices today; AM derives its own
# linear pricing from config (duration_ranges.AM) and TR from TL tariffs.
PRICES_COUNTRY_CODE = "GE"


def _same_origin_or_absent(request: Request) -> bool:
    """Browsers replay Basic-auth credentials on cross-site POSTs, so a
    price change whose Origin header names another site is rejected. An
    absent Origin (non-browser clients) is allowed, like the other admin
    forms."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc == request.url.netloc


def price_matrix(conn: sqlite3.Connection, settings) -> tuple[list[dict], list[dict]]:
    """(columns, rows) for the price table. Categories come from the
    canonical catalog (the same list the checkout offers), periods and
    default prices from config.yaml -- nothing enumerated here by hand."""
    categories = checkout_service.list_offered_categories(conn, settings, PRICES_COUNTRY_CODE)
    overrides = {
        (o.vehicle_category_code, o.period_code): o for o in price_overrides.list_overrides(conn, PRICES_COUNTRY_CODE)
    }
    columns: dict[str, str] = {}
    rows = []
    for category in categories:
        effective = {p.code: p for p in available_periods(settings, PRICES_COUNTRY_CODE, category.code)}
        cells = {}
        for default in config_periods(settings, PRICES_COUNTRY_CODE, category.code):
            columns.setdefault(default.code, default.label)
            cells[default.code] = {
                "field": f"price__{category.code}__{default.code}",
                "default_rub": default.price_rub,
                "effective_rub": effective[default.code].price_rub,
                "override": overrides.get((category.code, default.code)),
            }
        rows.append({"category": category, "cells": cells})
    return [{"code": code, "label": label} for code, label in columns.items()], rows


def _render(request: Request, conn, settings, *, errors=None, submitted=None, saved=None, status_code=200):
    columns, rows = price_matrix(conn, settings)
    return render(
        request,
        "admin_prices.html",
        {
            "columns": columns,
            "rows": rows,
            "errors": errors or {},
            "submitted": submitted or {},
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("/prices")
def get_admin_prices(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    saved = request.query_params.get("saved")
    return _render(request, conn, get_settings(), saved=int(saved) if saved and saved.isdigit() else None)


@router.post("/prices")
async def post_admin_prices(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    admin_username: str = Depends(current_admin_username),
):
    """All-or-nothing: every submitted cell is validated first, and any
    invalid value means nothing is saved. Only cells whose value actually
    changed are written. Only known (category, period) cells are ever read
    -- an unknown form field can't create a new product or period."""
    if not _same_origin_or_absent(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    settings = get_settings()
    form = await request.form()
    _, rows = price_matrix(conn, settings)

    errors: dict[str, str] = {}
    submitted: dict[str, str] = {}
    changes = []
    for row in rows:
        for period_code, cell in row["cells"].items():
            if cell["field"] not in form:
                continue
            raw = str(form[cell["field"]])
            submitted[cell["field"]] = raw
            price, error = parse_price(raw)
            if error:
                errors[cell["field"]] = error
            elif price != cell["effective_rub"]:
                changes.append((row["category"].code, period_code, cell["effective_rub"], price))

    if errors:
        return _render(request, conn, settings, errors=errors, submitted=submitted, status_code=422)

    for category_code, period_code, _old, new_price in changes:
        price_overrides.upsert_override(
            conn,
            country_code=PRICES_COUNTRY_CODE,
            vehicle_category_code=category_code,
            period_code=period_code,
            price_rub=new_price,
            updated_by=admin_username,
        )
    conn.commit()
    for category_code, period_code, old_price, new_price in changes:
        logger.info(
            "Retail price changed: %s/%s/%s %s -> %s RUB by admin %r",
            PRICES_COUNTRY_CODE, category_code, period_code, old_price, new_price, admin_username,
        )
    return RedirectResponse(f"/admin/prices?saved={len(changes)}", status_code=303)


@router.post("/prices/reset")
def post_admin_price_reset(
    request: Request,
    category_code: str = Form(...),
    period_code: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
    admin_username: str = Depends(current_admin_username),
):
    """Remove one override -- that cell goes back to the config.yaml price."""
    if not _same_origin_or_absent(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    removed = price_overrides.delete_override(
        conn, country_code=PRICES_COUNTRY_CODE, vehicle_category_code=category_code, period_code=period_code
    )
    conn.commit()
    if removed:
        logger.info(
            "Retail price override removed: %s/%s/%s by admin %r (config default applies)",
            PRICES_COUNTRY_CODE, category_code, period_code, admin_username,
        )
    return RedirectResponse("/admin/prices", status_code=303)
