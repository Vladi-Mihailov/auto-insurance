"""GE -> TPL policy issuance orchestration.

Confirmed real flow (manual browser-driven discovery, see task history for
the actual HAR evidence this is built from):

    our Order (PAID)
      -> live product resolution (GET /api/core/categories?embed=products)
      -> POST /api/policies                          (creates a real, numbered
                                                        TPL application)
      -> GET /ecommerce/bog?policyUId=...              (302 -> a real Bank of
                                                        Georgia payment URL)
      -> [STOP -- everything past this point is a human operator, in their
          own real browser, entering the company card and completing
          3DS2/OTP themselves]

This module never opens/calls mpi.gc.ge or any BOG endpoint
(/token, /start, /accept), never touches 3DS/ACS/OTP, and never uses
Playwright/Selenium -- every TPL-side call observed during discovery was a
plain, stateless, unauthenticated HTTP request, which is all this module
does (see app.integrations.tpl_ge.client).

Idempotency: exactly one tpl_uid is generated per Order, on the first call
only (see app.integrations.tpl_ge.repository.create_issuance's UNIQUE
constraints), and POST /api/policies is sent at most once per Order --
TplIssuance.application_already_created is the single guard every retry
path checks before ever building that request again. Refreshing an expired
BOG link (the "offer" session was observed to last ~5 minutes) means calling
GET /ecommerce/bog again with that SAME tpl_uid -- issue_tpl_policy already
does exactly that on every call, whether it's the first ("Оформить полис
TPL") or a later one ("Получить новую ссылку").
"""

import re
import sqlite3
import uuid
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from app.catalog import client as catalog_client
from app.catalog import repository as catalog_repo
from app.countries import match_citizenship_text
from app.integrations.tpl_ge import client as tpl_client
from app.integrations.tpl_ge import repository as tpl_repo
from app.integrations.tpl_ge.client import BogHandoffHttpError, PolicyLookupHttpError, TplPoliciesError
from app.integrations.tpl_ge.errors import (
    BogHandoffError,
    MissingRequiredDataError,
    PolicyNotReadyError,
    PolicyRetrievalError,
    ProductNotFoundError,
    TplApplicationError,
    TplIssuanceError,
    VisitorIdNotConfiguredError,
)
from app.integrations.tpl_ge.models import LiveProduct, TplIssuance
from app.orders.models import Order
from app.orders.repository import set_status
from app.orders.state_machine import OrderStatus
from app.settings import Settings

_PERIOD_CODE_RE = re.compile(r"^(\d+)d$")
_O_ID_QUERY_KEY = "o.id"


def extract_o_id(bog_payment_url: str) -> str | None:
    """Pulls the TPL-minted o.id back out of a stored bog_payment_url's own
    query string -- confirmed real query key is literally "o.id" (with the
    dot), not "o_id" or "oid". Returns None if the URL doesn't have one
    (defensive -- should never happen for a URL this module itself produced,
    but retrieve_issued_policy must not crash on a malformed/legacy value)."""
    query = parse_qs(urlsplit(bog_payment_url).query)
    values = query.get(_O_ID_QUERY_KEY)
    return values[0] if values else None


def fetch_live_products(client) -> list[dict]:
    """Raw categories (each with its own live products[]) -- reuses
    app.catalog.client's existing endpoint (it already fetches
    ?embed=products; app.catalog.sync just ignores that field, this module
    is the thing that actually reads it)."""
    return catalog_client.fetch_categories(client)


def resolve_product(
    raw_categories: list[dict], category_external_id: int, period_code: str | None, *, order_start_date: date
) -> LiveProduct:
    """Never hardcode a productId/price -- always resolved fresh against
    the just-fetched live catalog. Raises ProductNotFoundError (never
    guesses/falls back) if the category/period isn't found, or if it IS
    found but the order's start_date falls outside the live product's own
    minDate/maxDate purchase window."""
    match = _PERIOD_CODE_RE.match(period_code or "")
    if not match:
        raise ProductNotFoundError(f"Cannot resolve a TPL period from period_code={period_code!r}")
    period = int(match.group(1))
    period_type = "D"

    category = next((c for c in raw_categories if c.get("id") == category_external_id), None)
    if category is None:
        raise ProductNotFoundError(f"TPL category external_id={category_external_id} not found in the live catalog")

    for item in category.get("products", []):
        if item.get("period") == period and item.get("periodType") == period_type:
            min_date = date.fromisoformat(item["minDate"][:10])
            max_date = date.fromisoformat(item["maxDate"][:10])
            if not (min_date <= order_start_date <= max_date):
                raise ProductNotFoundError(
                    f"TPL product for period={period}{period_type} exists but the order's start_date "
                    f"{order_start_date.isoformat()} is outside its live purchase window "
                    f"({min_date.isoformat()}..{max_date.isoformat()})"
                )
            return LiveProduct(
                product_id=item["productId"],
                period=period,
                period_type=period_type,
                price_gel=Decimal(str(item["price"])),
                min_date=min_date,
                max_date=max_date,
            )
    raise ProductNotFoundError(
        f"No live TPL product for category external_id={category_external_id}, period={period}{period_type}"
    )


def resolve_citizenship_id(raw_countries: list[dict], citizenship_name: str | None) -> int:
    """citizenship_name -> TPL's numeric CitizenshipId. Reuses
    app.countries.match_citizenship_text (the same normalization already
    used for OCR results) to turn free text into one of our own canonical
    COUNTRIES names first, then looks that name up in TPL's own live
    country list by exact (case-insensitive) name match. Raises
    MissingRequiredDataError -- never guesses/defaults -- if either step
    fails to land on exactly one country."""
    canonical = match_citizenship_text(citizenship_name)
    if canonical is None:
        raise MissingRequiredDataError(f"Citizenship {citizenship_name!r} does not match any known country")
    target = canonical.strip().casefold()
    for item in raw_countries:
        if (item.get("name") or "").strip().casefold() == target:
            return item["id"]
    raise MissingRequiredDataError(f"Citizenship {canonical!r} has no matching entry in TPL's live country list")


def _require(value, field_label: str):
    if not value:
        raise MissingRequiredDataError(f"{field_label} is required for TPL issuance but is missing on this order")
    return value


def build_application_payload(
    order: Order,
    *,
    uid: str,
    product: LiveProduct,
    category_external_id: int,
    manufacturer_external_id: int,
    model_external_id: int,
    insurer_citizenship_id: int,
    owner_citizenship_id: int,
    driver_citizenship_id: int,
    visitor_id: str,
) -> dict:
    """Confirmed field set (real HAR evidence) -- do not add/rename fields.
    insurer=policyholder always (that's the only role our Order actually
    models under that name). Owner/driver fall back to the policyholder's
    own identity when *_same_as_policyholder is true (the DB stores NULL
    for those fields in that case -- see app.orders.models.Order -- so this
    is the one place that resolves the effective value TPL needs).

    MVP limitation, explicit rather than silently handled: a legal-entity
    vehicle owner is rejected outright -- only ever confirmed ("I") for a
    private individual in real discovery, and checkout itself currently
    keeps that option disabled."""
    _require(order.start_date, "start_date")
    _require(order.identifier, "VIN/chassis number")
    _require(order.car_number, "vehicle registration number")
    _require(order.full_name, "policyholder full name")
    _require(order.identification_number, "policyholder identification number")
    _require(order.contact_email, "policyholder email")
    _require(order.contact_phone, "policyholder phone")

    if not order.owner_same_as_policyholder and order.owner_entity_type == "legal":
        raise MissingRequiredDataError(
            "TPL issuance does not yet support a legal-entity vehicle owner (MVP limitation)"
        )

    owner_full_name = order.full_name if order.owner_same_as_policyholder else order.owner_full_name
    owner_identifier = order.identification_number if order.owner_same_as_policyholder else order.owner_identifier
    owner_email = order.contact_email if order.owner_same_as_policyholder else order.owner_email
    owner_phone = order.contact_phone if order.owner_same_as_policyholder else order.owner_phone
    _require(owner_full_name, "vehicle owner full name")
    _require(owner_identifier, "vehicle owner identification number")
    _require(owner_email, "vehicle owner email")
    _require(owner_phone, "vehicle owner phone")

    driver_full_name = order.full_name if order.driver_same_as_policyholder else order.driver_full_name
    driver_identifier = order.identification_number if order.driver_same_as_policyholder else order.driver_identifier
    driver_email = order.contact_email if order.driver_same_as_policyholder else order.driver_email
    driver_phone = order.contact_phone if order.driver_same_as_policyholder else order.driver_phone
    _require(driver_full_name, "driver full name")
    _require(driver_identifier, "driver identification number")
    _require(driver_email, "driver email")
    _require(driver_phone, "driver phone")

    return {
        "uId": uid,
        "startDate": order.start_date.isoformat(),
        "vinCode": order.identifier,
        "vehicleCategoryId": category_external_id,
        "vehicleRegistrationNumber": order.car_number,
        "vehicleManufacturerId": manufacturer_external_id,
        "vehicleManufacturerName": order.vehicle_make or "",
        "vehicleModelId": model_external_id,
        "vehicleModelName": order.vehicle_model or "",
        "productId": product.product_id,
        "insurerType": "I",
        "insurerTitle": order.full_name,
        "insurerIdentificationNumber": order.identification_number,
        "insurerEmail": order.contact_email,
        "insurerPhone": order.contact_phone,
        "insurerCitizenshipId": insurer_citizenship_id,
        "vehicleOwnerType": "I",
        "vehicleOwnerTitle": owner_full_name,
        "vehicleOwnerIdentificationNumber": owner_identifier,
        "vehicleOwnerEmail": owner_email,
        "vehicleOwnerPhone": owner_phone,
        "vehicleOwnerCitizenshipId": owner_citizenship_id,
        "vehicleDriverType": "I",
        "vehicleDriverTitle": driver_full_name,
        "vehicleDriverIdentificationNumber": driver_identifier,
        "vehicleDriverEmail": driver_email,
        "vehicleDriverPhone": driver_phone,
        "vehicleDriverCitizenshipId": driver_citizenship_id,
        "borderCrossId": None,
        "visitorId": visitor_id,
        "lang": "ru",
    }


def _resolve_catalog_external_ids(conn: sqlite3.Connection, order: Order) -> tuple[int, int, int]:
    category = catalog_repo.get_category_by_code(conn, order.vehicle_category_code) if order.vehicle_category_code else None
    if category is None:
        raise MissingRequiredDataError(f"Vehicle category {order.vehicle_category_code!r} not found in local catalog")
    manufacturer = catalog_repo.get_manufacturer(conn, order.manufacturer_id) if order.manufacturer_id else None
    if manufacturer is None:
        raise MissingRequiredDataError("Vehicle manufacturer not found in local catalog")
    model = catalog_repo.get_model(conn, order.model_id) if order.model_id else None
    if model is None:
        raise MissingRequiredDataError("Vehicle model not found in local catalog")
    return category.external_id, manufacturer.external_id, model.external_id


def _create_application(conn: sqlite3.Connection, order: Order, issuance: TplIssuance, settings: Settings) -> None:
    if not settings.tpl_ge.static_visitor_id:
        raise VisitorIdNotConfiguredError(
            "TPL_GE_STATIC_VISITOR_ID is not configured -- TPL issuance is disabled until it is set"
        )

    category_ext_id, manufacturer_ext_id, model_ext_id = _resolve_catalog_external_ids(conn, order)

    with tpl_client.new_client() as client:
        raw_categories = fetch_live_products(client)
        product = resolve_product(
            raw_categories, category_ext_id, order.period_code, order_start_date=order.start_date
        )

        raw_countries = catalog_client.fetch_countries(client)
        insurer_citizenship_id = resolve_citizenship_id(raw_countries, order.citizenship)
        owner_citizenship_name = order.citizenship if order.owner_same_as_policyholder else order.owner_citizenship
        owner_citizenship_id = resolve_citizenship_id(raw_countries, owner_citizenship_name)
        driver_citizenship_name = order.citizenship if order.driver_same_as_policyholder else order.driver_citizenship
        driver_citizenship_id = resolve_citizenship_id(raw_countries, driver_citizenship_name)

        payload = build_application_payload(
            order,
            uid=issuance.tpl_uid,
            product=product,
            category_external_id=category_ext_id,
            manufacturer_external_id=manufacturer_ext_id,
            model_external_id=model_ext_id,
            insurer_citizenship_id=insurer_citizenship_id,
            owner_citizenship_id=owner_citizenship_id,
            driver_citizenship_id=driver_citizenship_id,
            visitor_id=settings.tpl_ge.static_visitor_id,
        )

        try:
            tpl_client.create_application(client, payload)
        except TplPoliciesError as exc:
            raise TplApplicationError(f"TPL rejected the application: {exc}") from exc

    tpl_repo.mark_application_created(conn, order.id, tpl_product_id=product.product_id, tpl_purchase_price_gel=product.price_gel)


def _refresh_bog_link(conn: sqlite3.Connection, order: Order, issuance: TplIssuance, settings: Settings) -> None:
    params = {
        "lang": "ru",
        "policyUId": issuance.tpl_uid,
        "paymentType": "O",
        "payerTitle": order.full_name,
        "payerIdentificationNumber": order.identification_number,
        "returnUrl": f"https://tpl.ge/ru/policies/{issuance.tpl_uid}/success",
        "errorUrl": f"https://tpl.ge/ru/policies/{issuance.tpl_uid}/error",
    }
    with tpl_client.new_client() as client:
        try:
            location = tpl_client.initiate_bog_payment(client, params)
        except BogHandoffHttpError as exc:
            raise BogHandoffError(f"Could not obtain a BOG payment URL: {exc}") from exc

    tpl_repo.mark_bog_link_ready(conn, order.id, bog_payment_url=location, tpl_o_id=extract_o_id(location))


def issue_tpl_policy(conn: sqlite3.Connection, order: Order, settings: Settings) -> TplIssuance:
    """The one entry point the admin route calls -- for BOTH "Оформить полис
    TPL" (first click) and "Получить новую ссылку" (any later click). Both
    are the exact same idempotent operation: ensure the TPL application
    exists (create it only if it doesn't yet), then always get a fresh BOG
    payment URL for it."""
    if order.country_code != "GE":
        raise TplIssuanceError("TPL issuance is only available for Georgia (GE) orders")
    if order.status not in (OrderStatus.PAID.value, OrderStatus.PROCESSING.value):
        raise TplIssuanceError(f"Order must be PAID (or already in PROCESSING) to start TPL issuance, not {order.status!r}")

    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    if issuance is None:
        issuance = tpl_repo.create_issuance(conn, order.id, tpl_uid=str(uuid.uuid4()))

    if not issuance.application_already_created:
        try:
            _create_application(conn, order, issuance, settings)
        except TplIssuanceError as exc:
            tpl_repo.mark_failed(conn, order.id, error_message=str(exc))
            raise
        if order.status == OrderStatus.PAID.value:
            set_status(conn, order.id, OrderStatus.PROCESSING, note="TPL application created")
        issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
        assert issuance is not None

    try:
        _refresh_bog_link(conn, order, issuance, settings)
    except TplIssuanceError as exc:
        tpl_repo.record_error(conn, order.id, error_message=str(exc))
        raise

    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    assert issuance is not None
    return issuance


def report_operator_paid(conn: sqlite3.Connection, order: Order) -> TplIssuance:
    """"Оплата TPL завершена" -- purely a manual operator acknowledgement
    that they finished the BOG payment themselves. Never sets Order.status
    to POLICY_READY (the real success callback / PDF retrieval mechanism is
    still unexplored) -- only advances the issuance sub-status so the admin
    page can show "Ожидается получение полиса" instead of the payment
    button."""
    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    if issuance is None or not issuance.is_bog_link_ready:
        raise TplIssuanceError("No ready BOG payment link to confirm for this order")
    tpl_repo.mark_operator_reported_paid(conn, order.id)
    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    assert issuance is not None
    return issuance


# ---------------------------------------------------------------------------
# Post-payment policy retrieval (GET /api/policies/{o.id} and .../documents)
# ---------------------------------------------------------------------------
#
# NOT WIRED to any automatic trigger yet -- report_operator_paid() above is
# unchanged, still just a manual acknowledgement. retrieve_issued_policy is a
# standalone capability, callable explicitly (e.g. from a future admin
# action or an operator-triggered retry), deliberately not invoked from
# anywhere in this codebase yet. See the delivery report's OPEN ITEMS.

_DOCUMENT_TYPE_BY_KEYWORD = {
    "policy": "policy",
    "invoice": "invoice",
    "additional": "additional_terms",
}
_FILENAME_PREFIX_TO_KEY = {
    "policy-": "policy",
    "invoice-": "invoice",
    "additionalterms-": "additional_terms",
}


def _classify_documents(documents: list[dict]) -> dict[str, str]:
    """{"policy": url, "invoice": url, "additional_terms": url} -- best
    effort, never guessed. Confirmed real evidence: GET .../documents
    returns an explicit human-readable documentType ("Policy"/"Invoice"/
    "Additional"), which the INLINE documents[] on GET /api/policies/{o.id}
    itself does NOT include (only an opaque documentTypeId) -- documentType
    is checked first when present, falling back to the document's own
    "file" name prefix (confirmed real pattern: policy-/invoice-/
    additionalterms-) otherwise. A document matching neither is simply
    left out -- never assigned to a slot by guessing."""
    classified: dict[str, str] = {}
    for doc in documents:
        url = doc.get("url")
        if not url:
            continue
        doc_type = (doc.get("documentType") or "").strip().lower()
        filename = (doc.get("file") or "").strip().lower()

        key = None
        for keyword, slot in _DOCUMENT_TYPE_BY_KEYWORD.items():
            if keyword in doc_type:
                key = slot
                break
        if key is None:
            for prefix, slot in _FILENAME_PREFIX_TO_KEY.items():
                if filename.startswith(prefix):
                    key = slot
                    break
        if key is not None:
            classified.setdefault(key, url)
    return classified


def _looks_issued(policy: dict, documents: list[dict]) -> bool:
    """The one place that decides "issued" vs "not ready yet" -- exactly
    what TPL returns in the not-ready window is unconfirmed (see module
    docstring), so this is deliberately conservative: both a real
    policyNumber AND at least one document must be present."""
    return bool(policy.get("policyNumber")) and bool(documents)


def retrieve_issued_policy(conn: sqlite3.Connection, order: Order, settings: Settings, *, force: bool = False) -> TplIssuance:
    """GET /api/policies/{o.id} (+ .../documents for reliable document-type
    classification), and persist policyNumber/policyId/document URLs once
    confirmed issued.

    Idempotent by default: if this order's policy was already retrieved
    (TplIssuance.is_policy_retrieved), returns the cached row WITHOUT
    calling TPL again -- pass force=True to deliberately re-fetch. This is
    also the only guard against "aggressive polling": callers must invoke
    this explicitly (e.g. once, after an operator reports payment done),
    never in a retry loop -- nothing in this module loops or schedules
    itself.

    Raises PolicyNotReadyError (non-fatal -- try again later) if TPL
    responds but the policy doesn't look issued yet, and
    PolicyRetrievalError for a genuine anomaly (bad status, unparseable
    body, no o.id on file at all). Neither ever touches issuance_status --
    only mark_policy_retrieved's own columns change, on success."""
    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    if issuance is None or not issuance.tpl_o_id:
        raise TplIssuanceError("No TPL o.id on file for this order yet -- obtain a BOG payment link first")

    if issuance.is_policy_retrieved and not force:
        return issuance

    with tpl_client.new_client() as client:
        try:
            policy = tpl_client.fetch_policy(client, issuance.tpl_o_id)
        except PolicyLookupHttpError as exc:
            raise PolicyRetrievalError(f"GET /api/policies/{{o.id}} failed: {exc}") from exc

        documents = policy.get("documents") or []
        if not _looks_issued(policy, documents):
            raise PolicyNotReadyError("TPL has not finished issuing this policy yet")

        # The inline documents[] lacks a human-readable type -- fetch the
        # dedicated endpoint for reliable classification (see
        # client.fetch_policy_documents's own docstring). A failure here is
        # a real anomaly (the policy itself IS issued at this point) --
        # surfaced, never silently swallowed into a wrong classification.
        try:
            detailed_documents = tpl_client.fetch_policy_documents(client, issuance.tpl_o_id)
        except PolicyLookupHttpError as exc:
            raise PolicyRetrievalError(f"GET /api/policies/{{o.id}}/documents failed: {exc}") from exc

    classified = _classify_documents(detailed_documents or documents)

    tpl_repo.mark_policy_retrieved(
        conn,
        order.id,
        policy_number=policy["policyNumber"],
        tpl_policy_id=policy.get("policyId"),
        policy_document_url=classified.get("policy"),
        invoice_document_url=classified.get("invoice"),
        additional_terms_document_url=classified.get("additional_terms"),
    )

    issuance = tpl_repo.get_issuance_by_order_id(conn, order.id)
    assert issuance is not None
    return issuance


def download_policy_pdf(document_url: str) -> bytes:
    """Downloads a document using the exact URL TPL's own API returned
    (see TplIssuance.policy_document_url/invoice_document_url/
    additional_terms_document_url) -- never construct an ext-stream.tpl.ge
    path manually. Returns raw bytes; deliberately does not write them
    anywhere -- no production document-storage convention exists in this
    project yet (see the delivery report's OPEN ITEMS), so none is invented
    here. Not called from anywhere in this codebase yet."""
    with tpl_client.new_client() as client:
        return tpl_client.download_document(client, document_url)
