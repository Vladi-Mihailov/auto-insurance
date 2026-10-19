"""Admin retail-price editor (/admin/prices?country=GE|TR).

A thin UI over app.pricing.admin -- the SAME service the Telegram bot's
"💰 Цены" uses, so both surfaces share one set of rules (stale protection,
"saving the base removes the override", history). Prices are DB overrides on
top of config/config.yaml; config.yaml itself is never written from here.
Every checkout (website, every bot) reads the effective price through
app.pricing.provider, so a change applies to new calculations at once, with
no restart. Existing orders are never touched: each order keeps the
price_customer_minor it was created with.

One form per cell: each Save/Reset carries the value the manager saw, and a
cell changed by someone else in the meantime is refused with 409 instead of
being silently overwritten.

Protection: router-wide require_admin (HTTP Basic, fails closed when admin
credentials aren't configured), plus require_same_origin_post on every POST.
"""

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.deps import current_admin_username, get_db, get_settings, require_admin, require_same_origin_post
from app.pricing import admin as price_admin
from app.pricing.overrides import parse_price
from app.web.templating import render

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

# Countries whose prices are fixed-period config tariffs (editable here).
# AM is priced by a linear duration formula (duration_ranges.AM) -- nothing
# to override.
PRICE_COUNTRIES = {"GE": "🇬🇪 Грузия", "TR": "🇹🇷 Турция"}
DEFAULT_COUNTRY = "GE"

CHANNEL_LABELS = {price_admin.CHANNEL_WEB: "сайт", price_admin.CHANNEL_BOT: "бот"}


def _country_or_404(country: str | None) -> str:
    code = (country or DEFAULT_COUNTRY).upper()
    if code not in PRICE_COUNTRIES:
        raise HTTPException(status_code=404, detail="Unknown country")
    return code


def _format_updated_at(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return value


def _parse_expected(raw: str) -> int | None:
    """The hidden expected_* value the page rendered: blank = not priced /
    no override. Anything else non-numeric can only be a tampered form."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise HTTPException(status_code=400, detail="Malformed expected value")
    return int(raw)


def _cell_key(category_code: str, period_code: str) -> str:
    return f"{category_code}:{period_code}"


def _render(
    request: Request,
    conn: sqlite3.Connection,
    country: str,
    *,
    errors: dict | None = None,
    submitted: dict | None = None,
    saved_key: str | None = None,
    stale_key: str | None = None,
    alert: str | None = None,
    status_code: int = 200,
):
    """saved_key / stale_key: "<category>:<period>" of the cell just saved /
    refused as stale -- the template shows its CURRENT value from the
    matrix, read fresh for this response."""
    matrix = price_admin.price_matrix(conn, get_settings(), country)
    cells = {_cell_key(row.category_code, code): (row, cell) for row in matrix.rows for code, cell in row.cells.items()}
    return render(
        request,
        "admin_prices.html",
        {
            "country": country,
            "countries": PRICE_COUNTRIES,
            "matrix": matrix,
            "channel_labels": CHANNEL_LABELS,
            "errors": errors or {},
            "submitted": submitted or {},
            "saved": cells.get(saved_key) if saved_key else None,
            "stale": cells.get(stale_key) if stale_key else None,
            "stale_key": stale_key,
            "alert": alert,
            "format_updated_at": _format_updated_at,
        },
        status_code=status_code,
    )


@router.get("/prices")
def get_admin_prices(request: Request, country: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    return _render(request, conn, _country_or_404(country), saved_key=request.query_params.get("saved"))


@router.post("/prices/set", dependencies=[Depends(require_same_origin_post)])
def post_admin_price_set(
    request: Request,
    country: str = Form(...),
    category_code: str = Form(...),
    period_code: str = Form(...),
    expected_rub: str = Form(""),
    price: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
    admin_username: str = Depends(current_admin_username),
):
    code = _country_or_404(country)
    key = _cell_key(category_code, period_code)
    expected = _parse_expected(expected_rub)
    new_price, error = parse_price(price)
    if error:
        return _render(
            request, conn, code, errors={key: error}, submitted={key: price},
            alert="Цена не сохранена — исправьте отмеченное поле.", status_code=422,
        )
    try:
        price_admin.set_price(
            conn, get_settings(), country_code=code, category_code=category_code, period_code=period_code,
            expected_rub=expected, new_rub=new_price, actor=admin_username, source=price_admin.SOURCE_WEB,
        )
    except price_admin.UnknownCell:
        return _render(request, conn, code, alert="Неизвестная категория или период — ничего не сохранено.", status_code=422)
    except price_admin.StalePrice:
        return _render(request, conn, code, submitted={key: price}, stale_key=key, status_code=409)
    return RedirectResponse(f"/admin/prices?country={code}&saved={key}", status_code=303)


@router.post("/prices/reset", dependencies=[Depends(require_same_origin_post)])
def post_admin_price_reset(
    request: Request,
    country: str = Form(...),
    category_code: str = Form(...),
    period_code: str = Form(...),
    expected_override_rub: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
    admin_username: str = Depends(current_admin_username),
):
    """Remove one override -- that cell goes back to the config.yaml price."""
    code = _country_or_404(country)
    key = _cell_key(category_code, period_code)
    expected = _parse_expected(expected_override_rub)
    try:
        price_admin.reset_price(
            conn, get_settings(), country_code=code, category_code=category_code, period_code=period_code,
            expected_override_rub=expected, actor=admin_username, source=price_admin.SOURCE_WEB,
        )
    except price_admin.UnknownCell:
        return _render(request, conn, code, alert="Неизвестная категория или период — ничего не изменено.", status_code=422)
    except price_admin.StalePrice:
        return _render(request, conn, code, stale_key=key, status_code=409)
    return RedirectResponse(f"/admin/prices?country={code}&saved={key}", status_code=303)
