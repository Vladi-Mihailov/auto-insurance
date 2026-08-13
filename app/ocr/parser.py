"""Normalizes a provider-agnostic OcrResult into VehicleDataCandidates:
trims/uppercases text, picks vin-vs-chassis, and matches manufacturer/model
text against our LOCAL synced catalog (never a live tpl.ge call from here).

This is deliberately a separate stage from app.ocr.provider: whatever the
provider's internal recognition strategy, it hands over five raw text
fields, and everything below is provider-independent normalization +
catalog lookup -- swapping the provider later never touches this file.
"""

import difflib
import re
import sqlite3

from app.catalog import repository as catalog_repo
from app.catalog.sync import sync_models_on_demand
from app.ocr.models import OcrResult, VehicleDataCandidates

# Conservative fuzzy-match thresholds -- deliberately high (see module
# docstring in app.ocr.provider: OCR text is untrusted, and a wrong
# manufacturer_id is worse than leaving the picker empty for the user to
# fill in). No hardcoded brand dictionary/alias list; only generic
# case/punctuation/whitespace normalization plus a high-confidence fuzzy
# fallback.
_FUZZY_MATCH_THRESHOLD = 0.92
_FUZZY_MATCH_MARGIN = 0.05  # best match must clear the runner-up by this much


def normalize_registration_number(text: str | None) -> str | None:
    """Trim + uppercase only -- no country-specific format assumption, this
    vehicle may be registered anywhere."""
    if not text:
        return None
    value = re.sub(r"\s+", " ", text.strip()).upper()
    return value or None


def normalize_vin_candidate(text: str | None) -> str | None:
    """Uppercase, trim, and drop spaces/hyphens (safe formatting
    separators an OCR pass commonly introduces from a stamped/embossed
    VIN). Deliberately does NOT apply O/0, I/1, B/8, S/5 substitutions --
    that would silently rewrite an ambiguous read instead of showing it to
    the user for confirmation, which is exactly what we must not do."""
    if not text:
        return None
    value = re.sub(r"[\s\-]", "", text.strip()).upper()
    return value or None


# Chassis numbers get the same lenient normalization as VIN -- there's no
# confirmed universal chassis-number format either (see app/validation.py).
normalize_chassis_candidate = normalize_vin_candidate


def choose_identifier(vin: str | None, chassis: str | None) -> tuple[str | None, str | None]:
    """The vehicle form has exactly one selected identifier type -- never
    populate both. VIN takes precedence when both are somehow present."""
    normalized_vin = normalize_vin_candidate(vin)
    if normalized_vin:
        return "vin", normalized_vin
    normalized_chassis = normalize_chassis_candidate(chassis)
    if normalized_chassis:
        return "chassis", normalized_chassis
    return None, None


def _normalize_name_for_matching(name: str) -> str:
    value = name.strip().casefold()
    value = re.sub(r"[.,]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value


def _best_fuzzy_match(target: str, candidates: list[tuple[str, object]]) -> object | None:
    """candidates: list of (normalized_name, item). Returns the item if the
    best match clears both the absolute threshold and a margin over the
    runner-up -- an ambiguous near-tie is treated as no match at all."""
    if not candidates:
        return None
    scored = sorted(
        ((difflib.SequenceMatcher(None, target, normalized).ratio(), item) for normalized, item in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best_item = scored[0]
    if best_score < _FUZZY_MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and (best_score - scored[1][0]) < _FUZZY_MATCH_MARGIN:
        return None  # too close to the runner-up to be confident
    return best_item


def match_manufacturer_text(conn: sqlite3.Connection, manufacturer_text: str | None):
    """Exact match first (after case/punctuation/whitespace normalization),
    then a "starts with the catalog name as a whole word" check (catches
    "Toyota Motor Corporation" -> "Toyota"), then a conservative fuzzy
    fallback. Returns None -- never a guess -- when nothing clears the bar;
    the caller keeps manufacturer_text as a review hint regardless."""
    if not manufacturer_text:
        return None
    target = _normalize_name_for_matching(manufacturer_text)
    if not target:
        return None

    manufacturers = catalog_repo.list_manufacturers(conn)
    normalized = [(_normalize_name_for_matching(m.name), m) for m in manufacturers]

    exact = [m for norm, m in normalized if norm == target]
    if len(exact) == 1:
        return exact[0]

    prefix = [m for norm, m in normalized if norm and target.startswith(norm + " ")]
    if len(prefix) == 1:
        return prefix[0]

    return _best_fuzzy_match(target, normalized)


def match_model_text(conn: sqlite3.Connection, manufacturer_id: int, model_text: str | None):
    """Matched ONLY within the given manufacturer's own models -- never a
    global search across the whole catalog. Uses the existing on-demand
    sync mechanism (same as /api/vehicle-models) when this manufacturer's
    models have never been synced -- never a direct browser/here-to-tpl.ge
    call."""
    if not model_text:
        return None
    manufacturer = catalog_repo.get_manufacturer(conn, manufacturer_id)
    if manufacturer is None:
        return None
    if manufacturer.models_never_synced:
        if not sync_models_on_demand(conn, manufacturer):
            return None  # sync failed -- no models to match against right now

    target = _normalize_name_for_matching(model_text)
    if not target:
        return None
    models = catalog_repo.list_models(conn, manufacturer_id)
    normalized = [(_normalize_name_for_matching(m.name), m) for m in models]

    exact = [m for norm, m in normalized if norm == target]
    if len(exact) == 1:
        return exact[0]

    return _best_fuzzy_match(target, normalized)


def build_candidates(conn: sqlite3.Connection, ocr_result: OcrResult) -> VehicleDataCandidates:
    identifier_type, identifier = choose_identifier(ocr_result.vin, ocr_result.chassis_number)

    manufacturer = match_manufacturer_text(conn, ocr_result.manufacturer)
    model = None
    if manufacturer is not None:
        model = match_model_text(conn, manufacturer.id, ocr_result.model)

    return VehicleDataCandidates(
        registration_number=normalize_registration_number(ocr_result.registration_number),
        identifier_type=identifier_type,
        identifier=identifier,
        manufacturer_id=manufacturer.id if manufacturer else None,
        manufacturer_text=ocr_result.manufacturer,
        model_id=model.id if model else None,
        model_text=ocr_result.model,
    )
