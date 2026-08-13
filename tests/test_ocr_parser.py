"""Normalization + catalog-matching logic in app.ocr.parser -- entirely
provider-independent (fed synthetic OcrResult values, never a real
document/API call). See app.ocr.provider's FakeOcrProvider for the
provider-facing tests."""

import pytest

from app.catalog.repository import mark_models_synced, upsert_manufacturer, upsert_model
from app.db import get_connection, init_db
from app.ocr.models import OcrResult
from app.ocr.parser import (
    build_candidates,
    choose_identifier,
    match_manufacturer_text,
    match_model_text,
    normalize_registration_number,
    normalize_vin_candidate,
)


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


# --------------------------- VIN / registration ----------------------------


def test_vin_normalization_trims_uppercases_strips_separators():
    assert normalize_vin_candidate(" wvwzzz1jzxw000001 ") == "WVWZZZ1JZXW000001"
    assert normalize_vin_candidate("WVW-ZZZ 1JZ XW0 00001") == "WVWZZZ1JZXW000001"


def test_vin_normalization_does_not_apply_ocr_confusion_substitutions():
    # O/0, I/1, B/8, S/5 ambiguity must NOT be silently "corrected" here --
    # the candidate is shown to the user exactly as read, for them to judge.
    assert normalize_vin_candidate("WVWZZZ1JZXW0000O1") == "WVWZZZ1JZXW0000O1"


def test_vin_normalization_none_for_missing_or_blank():
    assert normalize_vin_candidate(None) is None
    assert normalize_vin_candidate("   ") is None


def test_registration_number_normalization_is_country_agnostic():
    assert normalize_registration_number(" ab-123-cd ") == "AB-123-CD"
    assert normalize_registration_number("XYZ 9999") == "XYZ 9999"  # not forced into a RU/GE-only shape


def test_choose_identifier_prefers_vin_when_both_present():
    identifier_type, identifier = choose_identifier("wvw123", "chs456")
    assert identifier_type == "vin"
    assert identifier == "WVW123"


def test_choose_identifier_falls_back_to_chassis_when_vin_absent():
    identifier_type, identifier = choose_identifier(None, "chs456")
    assert identifier_type == "chassis"
    assert identifier == "CHS456"


def test_choose_identifier_both_missing_is_none_none():
    assert choose_identifier(None, None) == (None, None)


# --------------------------- manufacturer matching --------------------------


def test_manufacturer_exact_normalized_match(conn):
    bmw_id = upsert_manufacturer(conn, external_id=1, name="BMW", is_popular=True)
    conn.commit()
    match = match_manufacturer_text(conn, "  bmw  ")
    assert match is not None and match.id == bmw_id


def test_manufacturer_prefix_match_for_legal_entity_full_name(conn):
    """OCR reading the document's full legal name ("Volkswagen AG") rather
    than just the brand must still resolve to our catalog's "VOLKSWAGEN"."""
    vw_id = upsert_manufacturer(conn, external_id=1, name="VOLKSWAGEN", is_popular=True)
    conn.commit()
    match = match_manufacturer_text(conn, "Volkswagen AG")
    assert match is not None and match.id == vw_id


def test_manufacturer_conservative_fuzzy_match_for_a_minor_typo(conn):
    mitsubishi_id = upsert_manufacturer(conn, external_id=1, name="MITSUBISHI", is_popular=True)
    conn.commit()
    match = match_manufacturer_text(conn, "MITSUBISCHI")  # single-letter OCR misread, ratio ~0.95
    assert match is not None and match.id == mitsubishi_id


def test_short_manufacturer_typo_is_too_ambiguous_for_the_conservative_threshold(conn):
    """"TOY0TA" vs "TOYOTA" is a single-character OCR confusion, but on a
    short brand name the difflib ratio (~0.83) does not clear our
    deliberately high, conservative threshold -- staying null (and showing
    the OCR text as a hint) is the correct, safer outcome here, not a bug."""
    upsert_manufacturer(conn, external_id=1, name="TOYOTA", is_popular=True)
    conn.commit()
    match = match_manufacturer_text(conn, "TOY0TA")
    assert match is None


def test_uncertain_manufacturer_text_stays_null_not_a_guess(conn):
    upsert_manufacturer(conn, external_id=1, name="MERCEDES-BENZ", is_popular=True)
    conn.commit()
    match = match_manufacturer_text(conn, "SomeCompletelyUnrelatedBrandXYZ")
    assert match is None


def test_manufacturer_text_missing_returns_none_without_querying_catalog(conn):
    assert match_manufacturer_text(conn, None) is None
    assert match_manufacturer_text(conn, "") is None
    assert match_manufacturer_text(conn, "   ") is None


# ------------------------------ model matching -------------------------------


def test_model_matched_only_inside_its_own_manufacturer_not_globally(conn):
    bmw_id = upsert_manufacturer(conn, external_id=1, name="BMW", is_popular=True)
    audi_id = upsert_manufacturer(conn, external_id=2, name="AUDI", is_popular=True)
    a4_id = upsert_model(conn, external_id=20, manufacturer_id=audi_id, name="A4")
    upsert_model(conn, external_id=10, manufacturer_id=bmw_id, name="730 LD")
    mark_models_synced(conn, bmw_id)
    mark_models_synced(conn, audi_id)
    conn.commit()

    # "A4" exists, but only under AUDI -- searching within BMW must not find it.
    assert match_model_text(conn, bmw_id, "A4") is None

    match = match_model_text(conn, audi_id, "a4")
    assert match is not None and match.id == a4_id


def test_model_uses_existing_on_demand_sync_mechanism_when_never_synced(conn, monkeypatch):
    """Never a direct browser/here-to-tpl.ge call -- reuses the same
    sync_models_on_demand the /api/vehicle-models route already uses."""
    from app.ocr import parser as parser_module

    manufacturer_id = upsert_manufacturer(conn, external_id=1, name="BMW", is_popular=True)
    conn.commit()

    def fake_sync(conn_, manufacturer):
        upsert_model(conn_, external_id=1, manufacturer_id=manufacturer.id, name="730 LD")
        mark_models_synced(conn_, manufacturer.id)
        conn_.commit()
        return True

    monkeypatch.setattr(parser_module, "sync_models_on_demand", fake_sync)
    match = match_model_text(conn, manufacturer_id, "730 LD")
    assert match is not None and match.name == "730 LD"


def test_model_text_missing_or_manufacturer_unresolved_returns_none(conn):
    bmw_id = upsert_manufacturer(conn, external_id=1, name="BMW", is_popular=True)
    mark_models_synced(conn, bmw_id)
    conn.commit()
    assert match_model_text(conn, bmw_id, None) is None
    assert match_model_text(conn, 999999, "730 LD") is None  # unknown manufacturer id


# ------------------------------ build_candidates ------------------------------


def test_build_candidates_full_success_resolves_everything(conn):
    bmw_id = upsert_manufacturer(conn, external_id=1, name="BMW", is_popular=True)
    model_id = upsert_model(conn, external_id=1, manufacturer_id=bmw_id, name="730 LD")
    mark_models_synced(conn, bmw_id)
    conn.commit()

    ocr_result = OcrResult(
        provider="fake",
        registration_number="ab123cd",
        vin="wvwzzz1jzxw000001",
        chassis_number=None,
        manufacturer="BMW",
        model="730 LD",
    )
    candidates = build_candidates(conn, ocr_result)
    assert candidates.registration_number == "AB123CD"
    assert candidates.identifier_type == "vin"
    assert candidates.identifier == "WVWZZZ1JZXW000001"
    assert candidates.manufacturer_id == bmw_id
    assert candidates.model_id == model_id


def test_build_candidates_partial_leaves_unmatched_fields_null_not_erroring(conn):
    upsert_manufacturer(conn, external_id=1, name="BMW", is_popular=True)
    conn.commit()

    ocr_result = OcrResult(
        provider="fake",
        registration_number=None,
        vin="wvwzzz1jzxw000001",
        chassis_number=None,
        manufacturer="SomeUnknownBrand",
        model=None,
    )
    candidates = build_candidates(conn, ocr_result)
    assert candidates.identifier == "WVWZZZ1JZXW000001"
    assert candidates.manufacturer_id is None
    assert candidates.manufacturer_text == "SomeUnknownBrand"  # kept as a review hint
    assert candidates.model_id is None


def test_build_candidates_model_never_attempted_when_manufacturer_unresolved(conn):
    """Section 13: model is searched ONLY after a manufacturer match --
    never a global fallback search when manufacturer_text didn't resolve."""
    upsert_manufacturer(conn, external_id=1, name="BMW", is_popular=True)
    other_id = upsert_manufacturer(conn, external_id=2, name="AUDI", is_popular=True)
    upsert_model(conn, external_id=1, manufacturer_id=other_id, name="A4")
    mark_models_synced(conn, other_id)
    conn.commit()

    ocr_result = OcrResult(
        provider="fake",
        registration_number=None,
        vin=None,
        chassis_number=None,
        manufacturer="UnrelatedBrandXYZ",
        model="A4",  # exists for AUDI, but manufacturer text never resolved
    )
    candidates = build_candidates(conn, ocr_result)
    assert candidates.manufacturer_id is None
    assert candidates.model_id is None
