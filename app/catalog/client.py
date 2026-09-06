"""Thin HTTP client for tpl.ge's public catalog-READ endpoints.

This is the only module that talks to tpl.ge for catalog browsing. It exists
purely to feed app.catalog.sync — the checkout backend/frontend never calls
tpl.ge directly (see app.catalog.sync / app.catalog.repository for the local,
synced catalog that the checkout actually reads from).

The real-money side-effecting calls (POST /api/policies, the ecommerce-api.tpl.ge
BOG payment handoff) are a different concern with a different risk profile —
see app.integrations.tpl_ge, which owns those instead of living here.

Endpoints were found by observing tpl.ge/ru/policies' own network requests
(a normal page load + normal UI interaction — the same requests its own
frontend makes), not reverse-engineered from anything private:

- GET /api/core/categories?embed=products  -> vehicle categories (+ their own
  GEL pricing/products[], which app.catalog.sync ignores — our RUB prices are
  our own — but which app.integrations.tpl_ge.service DOES read, to resolve
  the live TPL productId/price for actual policy purchase).
- GET /api/core/vehicles/manufacturers      -> full manufacturer list.
- GET /api/core/vehicles/manufacturers/{id}/models -> models for one manufacturer.
- GET /api/core/countries                   -> countries list (id/name), used
  by app.integrations.tpl_ge.service to resolve a citizenship name to TPL's
  numeric CitizenshipId. Not synced into a local table (app.catalog has no
  countries catalog) — fetched fresh, same freshness reasoning as products.
"""

import httpx

BASE_URL = "https://web-back.tpl.ge/api/core"
_TIMEOUT = 30.0
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; auto-insurance-catalog-sync/1.0)",
    "Accept": "application/json",
}


class TplClientError(httpx.HTTPError):
    """Non-2xx response from tpl.ge. Subclasses httpx.HTTPError (rather than
    a plain RuntimeError) so callers can catch one exception type for both
    network-level failures (timeouts, connection errors — raised by httpx
    itself) and this one — see app.catalog.sync's retry/on-demand logic."""

    pass


def _get(client: httpx.Client, path: str) -> list[dict]:
    response = client.get(f"{BASE_URL}{path}")
    if response.status_code != 200:
        raise TplClientError(f"GET {path} -> {response.status_code}")
    return response.json()


def fetch_categories(client: httpx.Client) -> list[dict]:
    """Each item: id, name, key, vehiclecategoryIcon, products[] (ignored by sync)."""
    return _get(client, "/categories?embed=products")


def fetch_manufacturers(client: httpx.Client) -> list[dict]:
    """Each item: id, name, number, isPopular."""
    return _get(client, "/vehicles/manufacturers")


def fetch_models(client: httpx.Client, manufacturer_external_id: int) -> list[dict]:
    """Each item: id, name — scoped to one manufacturer by its external id."""
    return _get(client, f"/vehicles/manufacturers/{manufacturer_external_id}/models")


def fetch_countries(client: httpx.Client) -> list[dict]:
    """Each item: id, name, number, isPopular. Names are English (e.g.
    "Georgia", "Russia") — the same convention app.countries.COUNTRIES uses
    for the policyholder's own citizenship field."""
    return _get(client, "/countries")


def new_client(timeout: float = _TIMEOUT) -> httpx.Client:
    return httpx.Client(headers=_HEADERS, timeout=timeout)
