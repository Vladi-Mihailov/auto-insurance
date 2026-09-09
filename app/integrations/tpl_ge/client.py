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


class PolicyLookupHttpError(httpx.HTTPError):
    """Non-2xx or unparseable-JSON response from GET /api/policies/{o.id}
    or GET /api/policies/{o.id}/documents. Raised for the HTTP layer only --
    "not issued yet" is NOT this (that's a 200 with an incomplete body,
    handled one layer up in app.integrations.tpl_ge.service, since only it
    knows what "incomplete" means for this endpoint)."""


class DocumentDownloadError(httpx.HTTPError):
    """The document URL TPL itself provided did not respond the way a real
    PDF should (wrong status, wrong Content-Type, or an empty body)."""


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


def fetch_policy(client: httpx.Client, o_id: str) -> dict:
    """GET /api/policies/{o.id}. Confirmed real behaviour (HAR evidence,
    already-issued case only -- see module docstring): HTTP 200, JSON body
    with policyNumber/policyId/dates/an inline documents[] array, no
    Cookie/Authorization required. What this looks like BEFORE issuance
    completes is NOT confirmed -- this function only validates the HTTP
    layer (status + JSON-parseable); classifying "issued" vs "not ready
    yet" is app.integrations.tpl_ge.service.retrieve_issued_policy's job,
    one layer up, since that requires business knowledge this thin client
    deliberately doesn't have."""
    response = client.get(f"{POLICIES_URL}/{o_id}")
    if response.status_code != 200:
        raise PolicyLookupHttpError(f"GET /api/policies/{{o.id}} -> {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise PolicyLookupHttpError(f"GET /api/policies/{{o.id}} -> unparseable JSON body: {exc}") from exc


def fetch_policy_documents(client: httpx.Client, o_id: str) -> list[dict]:
    """GET /api/policies/{o.id}/documents. Confirmed to return the same
    documents as fetch_policy's own inline documents[], but WITH an
    explicit human-readable documentType ("Policy"/"Invoice"/"Additional")
    the inline array lacks -- see service._classify_documents, the only
    caller, for why this dedicated call is worth making rather than relying
    on the inline array alone."""
    response = client.get(f"{POLICIES_URL}/{o_id}/documents")
    if response.status_code != 200:
        raise PolicyLookupHttpError(f"GET /api/policies/{{o.id}}/documents -> {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise PolicyLookupHttpError(f"GET /api/policies/{{o.id}}/documents -> unparseable JSON body: {exc}") from exc


def download_document(client: httpx.Client, document_url: str) -> bytes:
    """Downloads a document using the EXACT url TPL's own response provided
    -- never construct an ext-stream.tpl.ge path manually (see module
    docstring / delivery report). Validates status 200, an application/pdf
    Content-Type, and a non-empty body; raises DocumentDownloadError
    otherwise. Returns raw bytes -- this function has no opinion on where
    they get stored (see the delivery report's OPEN ITEMS: no production
    storage convention exists yet, so none is invented here)."""
    response = client.get(document_url)
    if response.status_code != 200:
        raise DocumentDownloadError(f"GET {document_url} -> {response.status_code} (expected 200)")
    content_type = response.headers.get("content-type", "")
    if "application/pdf" not in content_type.lower():
        raise DocumentDownloadError(f"GET {document_url} -> unexpected Content-Type {content_type!r} (expected application/pdf)")
    content = response.content
    if not content:
        raise DocumentDownloadError(f"GET {document_url} -> empty response body")
    return content
