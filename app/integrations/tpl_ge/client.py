"""Thin HTTP client for TPL's real (side-effecting) policy-issuance API and
the Bank of Georgia payment handoff.

Deliberately separate from app.catalog.client (which only ever reads
catalog/reference data): every call here has a real-world side effect --
POST /api/policies creates an actual, numbered TPL application, and the BOG
handoff registers a real payment session. Confirmed by manual browser-driven
discovery (real HAR captures, one deliberately controlled server-side POST,
never repeated) -- see the discovery task history for exactly what was
observed. Ordinary requests library conventions apply: httpx.Client does not
follow redirects unless explicitly told to (follow_redirects=True), which is
exactly the behaviour initiate_bog_payment needs and relies on.

This module never opens/calls mpi.gc.ge, BOG's /token, /start, /accept, or
anything 3DS/ACS/OTP-related -- that part of the flow is, and must remain,
a human operator acting in their own real browser (see
app.integrations.tpl_ge.service module docstring).
"""

import httpx

POLICIES_URL = "https://web-back.tpl.ge/api/policies"
BOG_HANDOFF_URL = "https://ecommerce-api.tpl.ge/ecommerce/bog"
_TIMEOUT = 30.0
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; auto-insurance-tpl-issuance/1.0)",
    "Accept": "application/json",
}


class TplPoliciesError(httpx.HTTPError):
    """Non-2xx (or otherwise unexpected) response from POST /api/policies."""


class BogHandoffHttpError(httpx.HTTPError):
    """GET /ecommerce/bog did not respond with the expected 302 + Location."""


def new_client(timeout: float = _TIMEOUT) -> httpx.Client:
    # follow_redirects defaults to False on httpx.Client -- relied on
    # explicitly by create_application (a 3xx there would be unexpected) and
    # by initiate_bog_payment (a 302 here must be captured, never followed).
    return httpx.Client(headers=_HEADERS, timeout=timeout)


def create_application(client: httpx.Client, payload: dict) -> None:
    """POST /api/policies. Confirmed real behaviour: success is HTTP 200
    with a genuinely empty body (observed identically across a real browser
    session and a separate controlled server-side POST) -- there is nothing
    to parse or return. Raises TplPoliciesError on any other status."""
    response = client.post(POLICIES_URL, json=payload)
    if response.status_code != 200:
        raise TplPoliciesError(f"POST /api/policies -> {response.status_code}")


def initiate_bog_payment(client: httpx.Client, params: dict) -> str:
    """GET /ecommerce/bog?... Confirmed real behaviour: success is HTTP 302
    with a Location header pointing at mpi.gc.ge -- this function returns
    that URL and nothing more. Never follows it. Raises BogHandoffHttpError
    if the status isn't 302 or Location is missing."""
    response = client.get(BOG_HANDOFF_URL, params=params, follow_redirects=False)
    if response.status_code != 302:
        raise BogHandoffHttpError(f"GET /ecommerce/bog -> {response.status_code} (expected 302)")
    location = response.headers.get("location")
    if not location:
        raise BogHandoffHttpError("GET /ecommerce/bog -> 302 with no Location header")
    return location
