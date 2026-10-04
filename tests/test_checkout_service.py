"""app.checkout -- the transport-independent checkout rules/steps shared by
the web checkout and the Telegram bot.

Each test gets its own fresh SQLite file (init_db on tmp_path) so draft/
analytics assertions are exact. Dates are always relative to
today_in_georgia(), never literals."""

import json
from datetime import timedelta

import pytest

from app.catalog.repository import upsert_category
from app.checkout import rules
from app.checkout import service as checkout_service
from app.checkout.service import DraftStepMissing
from app.dates.rules import GeorgiaDateRule, today_in_georgia
from app.db import get_connection, init_db
from app.deps import PROJECT_ROOT
from app.formatting import format_rub
from app.pricing.provider import available_periods
from app.sessions.repository import ensure_session, get_draft, merge_draft
from app.settings import load_settings
from app.web import checkout_routes

_GE_CATEGORIES = [
    (7, "passenger_car", "Легковой"),
    (10, "motorcycle", "Мотоцикл"),
    (9, "bus", "Автобус"),
    (8, "truck", "Грузовик"),
    (11, "trailer", "Прицеп"),
    (12, "special_vehicle", "Спецтехника"),
]


@pytest.fixture
def production_settings(monkeypatch):
    """The REAL config/config.yaml (canonical business prices), never the
    tests/fixtures dummy pricing -- .env is still never loaded (see
    tests/conftest.py)."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def conn(tmp_path):
    db_file = tmp_path / "service.db"
    init_db(db_file)
    connection = get_connection(db_file)
    for external_id, code, name in _GE_CATEGORIES:
        upsert_category(connection, external_id=external_id, code=code, name=name, icon=None)
    connection.commit()
    yield connection
    connection.close()


def _session(conn, session_id="svc-1", country_code="GE"):
    ensure_session(conn, session_id)
    merge_draft(conn, session_id, {"country_code": country_code})
    return session_id


# ------------------------------- web compatibility ----------------------------


def test_web_module_reexports_the_shared_rules_not_copies():
    assert checkout_routes._draft_country_code is rules.draft_country_code
    assert checkout_routes._allowed_category_codes is rules.allowed_category_codes
    assert checkout_routes._fixed_duration_date_rule is rules.fixed_duration_date_rule
    assert checkout_routes._parse_and_validate_start_date is rules.parse_and_validate_start_date
    assert checkout_routes._category_period_step_completed is rules.category_period_step_completed
    assert checkout_routes.SUPPORTED_COUNTRY_CODES is rules.SUPPORTED_COUNTRY_CODES


def test_format_rub_matches_existing_web_filter():
    from app.web.templating import templates

    assert format_rub(2149) == "2 149"
    assert format_rub(849) == "849"
    assert templates.env.filters["rub"](12345) == "12 345"


# ------------------------------- categories -----------------------------------


def test_georgia_offers_all_six_catalog_categories(conn, production_settings):
    codes = [c.code for c in checkout_service.list_offered_categories(conn, production_settings, "GE")]
    assert sorted(codes) == sorted(code for _, code, _ in _GE_CATEGORIES)


def test_country_restriction_comes_from_config(conn, production_settings):
    allowed = production_settings.catalog.enabled_category_codes_by_country["TR"]
    codes = [c.code for c in checkout_service.list_offered_categories(conn, production_settings, "TR")]
    assert sorted(codes) == sorted(allowed)


# ------------------------------- periods / prices -----------------------------


@pytest.mark.parametrize("category_code", [code for _, code, _ in _GE_CATEGORIES])
def test_priced_periods_are_exactly_the_canonical_provider_output(production_settings, category_code):
    expected = [p for p in available_periods(production_settings, "GE", category_code) if p.is_priced]
    assert checkout_service.list_priced_periods(production_settings, "GE", category_code) == expected
    assert [p.code for p in expected] == ["15d", "30d", "90d"]


def test_known_ge_prices_via_provider(production_settings):
    def prices(category):
        return {p.code: p.price_rub for p in checkout_service.list_priced_periods(production_settings, "GE", category)}

    assert prices("passenger_car") == {"15d": 1349, "30d": 2149, "90d": 3649}
    assert prices("motorcycle") == {"15d": 1059, "30d": 1549, "90d": 2899}
    assert prices("trailer") == {"15d": 849, "30d": 1249, "90d": 1799}


# ------------------------------- select_category_period -----------------------


def test_select_category_period_writes_draft_and_logs_event(conn, production_settings):
    session_id = _session(conn)
    result = checkout_service.select_category_period(
        conn, production_settings, session_id=session_id, category_code="passenger_car", period_code="30d"
    )
    assert result.ok
    assert result.period.price_rub == 2149
    draft = get_draft(conn, session_id)
    assert draft["vehicle_category_code"] == "passenger_car"
    assert draft["period_code"] == "30d"
    assert draft["price_customer_minor"] == 214900
    row = conn.execute(
        "SELECT properties FROM insurance_analytics_events WHERE session_id = ? AND event_name = 'category_period_selected'",
        (session_id,),
    ).fetchone()
    assert json.loads(row["properties"]) == {"category": "passenger_car", "period": "30d"}


def test_unknown_category_rejected_without_touching_draft(conn, production_settings):
    session_id = _session(conn)
    result = checkout_service.select_category_period(
        conn, production_settings, session_id=session_id, category_code="spaceship", period_code="30d"
    )
    assert result.error == "Выберите категорию транспорта"
    assert "vehicle_category_code" not in get_draft(conn, session_id)


def test_category_not_enabled_for_country_is_rejected(conn, production_settings):
    session_id = _session(conn, country_code="TR")
    result = checkout_service.select_category_period(
        conn, production_settings, session_id=session_id, category_code="bus", period_code="30d"
    )
    assert result.error == "Выберите категорию транспорта"


def test_unknown_period_rejected(conn, production_settings):
    session_id = _session(conn)
    result = checkout_service.select_category_period(
        conn, production_settings, session_id=session_id, category_code="passenger_car", period_code="1y"
    )
    assert result.error == "Выберите один из доступных периодов"
    assert "period_code" not in get_draft(conn, session_id)


def test_changing_period_recomputes_existing_end_date(conn, production_settings):
    session_id = _session(conn)
    start = today_in_georgia() + timedelta(days=3)
    checkout_service.select_category_period(
        conn, production_settings, session_id=session_id, category_code="passenger_car", period_code="15d"
    )
    checkout_service.set_fixed_period_start_date(
        conn, production_settings, session_id=session_id, start_date=start, today=today_in_georgia()
    )
    checkout_service.select_category_period(
        conn, production_settings, session_id=session_id, category_code="passenger_car", period_code="90d"
    )
    assert get_draft(conn, session_id)["end_date"] == (start + timedelta(days=90)).isoformat()


# ------------------------------- start date -----------------------------------


def _with_period(conn, settings, period_code="30d"):
    session_id = _session(conn)
    checkout_service.select_category_period(
        conn, settings, session_id=session_id, category_code="passenger_car", period_code=period_code
    )
    return session_id


@pytest.mark.parametrize("offset_days", [0, 1, 45])
def test_start_date_today_or_later_accepted_with_georgia_end_date(conn, production_settings, offset_days):
    session_id = _with_period(conn, production_settings)
    today = today_in_georgia()
    start = today + timedelta(days=offset_days)
    result = checkout_service.set_fixed_period_start_date(
        conn, production_settings, session_id=session_id, start_date=start, today=today
    )
    assert result.ok
    assert result.end_date == GeorgiaDateRule().compute_end_date(start, "30d")
    draft = get_draft(conn, session_id)
    assert draft["start_date"] == start.isoformat()
    assert draft["end_date"] == result.end_date.isoformat()


def test_past_start_date_rejected_and_not_stored(conn, production_settings):
    session_id = _with_period(conn, production_settings)
    today = today_in_georgia()
    result = checkout_service.set_fixed_period_start_date(
        conn, production_settings, session_id=session_id, start_date=today - timedelta(days=1), today=today
    )
    assert result.error == "Дата начала не может быть раньше сегодняшнего дня"
    assert "start_date" not in get_draft(conn, session_id)


def test_start_date_before_period_selection_is_a_step_error(conn, production_settings):
    session_id = _session(conn)
    with pytest.raises(DraftStepMissing):
        checkout_service.set_fixed_period_start_date(
            conn, production_settings, session_id=session_id, start_date=today_in_georgia(), today=today_in_georgia()
        )


def test_iso_parser_and_date_validator_agree():
    today = today_in_georgia()
    assert rules.parse_and_validate_start_date("not-a-date", today) == (None, "Некорректная дата")
    yesterday = today - timedelta(days=1)
    assert rules.parse_and_validate_start_date(yesterday.isoformat(), today) == rules.validate_start_date(yesterday, today)
    assert rules.parse_and_validate_start_date(today.isoformat(), today) == (today, None)


# ------------------------------------------------ phase 4 shared rules


def test_merge_ocr_update_only_fills_gaps_and_reports_conflicts():
    draft = {"registration_number": "AB123CD", "manufacturer_id": 1, "model_id": None, "identifier": None}
    update = {
        "registration_number": "ZZ999ZZ",
        "identifier_type": "vin",
        "identifier": "VIN12345",
        "manufacturer_id": 1,
        "model_id": 7,
        "ocr_manufacturer_hint": None,
        "ocr_model_hint": None,
        "ocr_policyholder_full_name": None,
    }
    writes, conflicts = checkout_service.merge_ocr_update(draft, update)
    assert "registration_number" not in writes and conflicts == ["registration_number"]
    assert (writes["identifier_type"], writes["identifier"], writes["model_id"]) == ("vin", "VIN12345", 7)
    assert writes["data_entry_method"] == "documents"


def test_merge_ocr_update_never_clears_or_downgrades():
    draft = {"manufacturer_id": 1, "model_id": 7, "identifier_type": "vin", "identifier": "VIN12345", "registration_number": "AB"}
    update = {k: None for k in ("registration_number", "identifier_type", "identifier", "manufacturer_id", "model_id")}
    update["ocr_manufacturer_hint"] = "Unknown Brand"  # unmatched text must not replace a catalog match
    writes, conflicts = checkout_service.merge_ocr_update(draft, update)
    assert writes == {"data_entry_method": "documents"} and conflicts == []


def test_merge_ocr_update_different_manufacturer_is_a_conflict_not_a_switch():
    writes, conflicts = checkout_service.merge_ocr_update({"manufacturer_id": 1, "model_id": 7}, {"manufacturer_id": 2, "model_id": 9})
    assert "manufacturer_id" not in writes and "model_id" not in writes and conflicts == ["manufacturer"]


def test_combine_ocr_results_prefers_primary():
    from app.ocr.models import OcrResult, combine_ocr_results

    primary = OcrResult(provider="p", registration_number="A1", vin=None, chassis_number=None, manufacturer="Toyota", model=None)
    secondary = OcrResult(provider="s", registration_number="B2", vin="V", chassis_number=None, manufacturer="BMW", model="X5")
    combined = combine_ocr_results(primary, secondary)
    assert (combined.provider, combined.registration_number, combined.vin, combined.manufacturer, combined.model) == (
        "p", "A1", "V", "Toyota", "X5"
    )


def test_vehicle_missing_fields_applies_the_existing_completion_rule(conn):
    from app.catalog.repository import mark_models_synced, upsert_manufacturer, upsert_model

    manufacturer_id = upsert_manufacturer(conn, external_id=1, name="TOYOTA", is_popular=True)
    model_id = upsert_model(conn, external_id=1, manufacturer_id=manufacturer_id, name="CAMRY")
    other_id = upsert_model(conn, external_id=-1, manufacturer_id=manufacturer_id, name="Other")
    mark_models_synced(conn, manufacturer_id)
    complete = {
        "registration_number": "AB123CD",
        "identifier_type": "chassis",
        "identifier": "FR123",
        "manufacturer_id": manufacturer_id,
        "model_id": model_id,
    }
    assert checkout_service.vehicle_missing_fields(conn, complete) == []
    assert checkout_service.vehicle_missing_fields(conn, {**complete, "model_id": other_id}) == []
    assert checkout_service.vehicle_missing_fields(conn, {**complete, "identifier": None}) == ["identifier"]
    assert checkout_service.vehicle_missing_fields(conn, {**complete, "model_id": 99999}) == ["model"]
    assert checkout_service.vehicle_missing_fields(conn, {**complete, "manufacturer_id": None}) == ["manufacturer", "model"]


def test_policyholder_phone_is_required_but_uses_existing_validator():
    assert checkout_service.validate_policyholder_field("contact_phone", "") == (None, "Телефон: заполните это поле")
    assert checkout_service.validate_policyholder_field("contact_phone", "+79001234567") == ("+79001234567", None)
    assert checkout_service.validate_policyholder_field("full_name", "Иванов")[1].startswith("ФИО")
    missing = checkout_service.policyholder_missing_fields({"full_name": "Ivanov Ivan", "contact_phone": ""})
    assert missing == ["identification_number", "citizenship", "contact_email", "contact_phone"]


def test_refresh_draft_price_follows_an_override_and_never_unprices(conn, production_settings, tmp_path, monkeypatch):
    from app.pricing import overrides

    settings = production_settings.model_copy(update={"app": production_settings.app.model_copy(update={"db_file": tmp_path / "service.db"})})
    session_id = _with_period(conn, settings)
    overrides.upsert_override(
        conn, country_code="GE", vehicle_category_code="passenger_car", period_code="30d", price_rub=1111, updated_by=None
    )
    conn.commit()
    assert checkout_service.refresh_draft_price(conn, settings, session_id=session_id) == 111100
    assert get_draft(conn, session_id)["price_customer_minor"] == 111100
    merge_draft(conn, session_id, {"period_code": "7d"})  # a period the provider doesn't know
    assert checkout_service.refresh_draft_price(conn, settings, session_id=session_id) == 111100
