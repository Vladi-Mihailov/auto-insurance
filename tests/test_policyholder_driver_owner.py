"""Policyholder OCR autofill (full_name/identification_number/citizenship)
and the Driver/Owner blocks ("Водитель"/"Владелец", tpl.ge parity) --
FakeOcrProvider only, no test here ever touches a real Vision API.

"passenger_car" is re-seeded with the same external_id=7 already used by
test_routes_smoke.py -- upsert_category is a true idempotent upsert, so
this is safe regardless of test collection order.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import get_ocr_provider, get_settings
from app.main import app
from app.ocr.models import OcrResult
from app.ocr.provider import FakeOcrProvider
from policyholder_helpers import valid_policyholder_data as _minimal_policyholder_data

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
# external_id chosen to not collide with other test files' ranges.
_manufacturer_id = upsert_manufacturer(_conn, external_id=14001, name="ZDRIVEROWNERFICTIONALMAKE", is_popular=True)
_model_id = upsert_model(_conn, external_id=14001, manufacturer_id=_manufacturer_id, name="ZDRIVEROWNERFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


def _make_jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (150, 20, 20)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _reach_vehicle_manual(client_):
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2031-08-20"})
    client_.post("/method", data={"choice": "manual"})
    client_.post(
        "/vehicle",
        data={
            "registration_number": "DO001AA",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000900",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )


def _reach_policyholder_via_ocr(client_, fake_provider, ocr_result):
    fake_provider["provider"] = FakeOcrProvider(result=ocr_result)
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2031-08-20"})
    client_.post("/method", data={"choice": "documents"})
    client_.post(
        "/documents-soon",
        files=[("files", ("doc.jpg", _make_jpeg_bytes(), "image/jpeg"))],
    )
    client_.post(
        "/vehicle",
        data={
            "registration_number": "DO002BB",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000901",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )


@pytest.fixture
def fake_provider():
    holder = {"provider": FakeOcrProvider()}
    app.dependency_overrides[get_ocr_provider] = lambda: holder["provider"]
    yield holder
    app.dependency_overrides.pop(get_ocr_provider, None)


# --------------------------- OCR policyholder autofill -------------------------


def test_ocr_policyholder_full_name_autofills_the_name_field(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            policyholder_full_name="Petrov Petr",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'value="Petrov Petr"' in response.text


def test_ocr_identification_number_autofills_the_field(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            passport_number="AB1234567",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'value="AB1234567"' in response.text


def test_ocr_citizenship_autoselects_the_matching_country(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            citizenship="Georgia",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert '<option value="Georgia" selected>' in response.text


def test_ocr_unrecognizable_citizenship_selects_nothing(fake_provider):
    """Never invent/guess a country -- see app.countries.match_citizenship_text."""
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            citizenship="Not A Real Country Xyz",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert "selected>" not in response.text  # only the disabled placeholder would match otherwise
    assert '<option value="" ' in response.text


def test_missing_ocr_fields_leave_the_form_empty(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'name="full_name" value=""' in response.text
    assert 'name="identification_number" value=""' in response.text
    assert "selected>" not in response.text  # no country pre-selected either


def test_ocr_missing_identification_must_be_filled_manually_before_submit(fake_provider):
    """Absence of OCR autofill is never itself an error -- but the field is
    still required, so submitting without filling it in by hand fails
    exactly like any other missing required field."""
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            # no policyholder_full_name/passport_number/citizenship at all
        ),
    )
    response = client_.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "contact_email": "ivan@example.com"},  # identification/citizenship left blank
    )
    assert response.status_code == 422


def test_ocr_unknown_citizenship_requires_manual_correction_before_submit(fake_provider):
    """Section 8: OCR read a citizenship it couldn't safely match to a
    country in our list -- nothing gets pre-selected (see
    test_ocr_unrecognizable_citizenship_selects_nothing), and submitting
    without the user picking one manually still fails required validation
    -- never a silently-accepted blank citizenship."""
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            citizenship="Not A Real Country Xyz",
        ),
    )
    response = client_.post(
        "/policyholder",
        data={"full_name": "Ivanov Ivan", "identification_number": "AB1234567", "contact_email": "ivan@example.com"},
    )
    assert response.status_code == 422
    assert "Гражданство" in response.text


def test_missing_identification_number_is_rejected(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post("/policyholder", data=_minimal_policyholder_data(identification_number=""))
    assert response.status_code == 422


def test_missing_citizenship_is_rejected(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post("/policyholder", data=_minimal_policyholder_data(citizenship=""))
    assert response.status_code == 422


def test_arbitrary_citizenship_not_in_allowlist_is_rejected(fake_provider):
    """A form-tampered/stale submission can't write an invented country --
    only exact app.countries.COUNTRIES entries are accepted."""
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post("/policyholder", data=_minimal_policyholder_data(citizenship="Wakanda"))
    assert response.status_code == 422


def test_valid_full_submission_with_identification_and_citizenship_is_accepted(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post("/policyholder", data=_minimal_policyholder_data(), follow_redirects=False)
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "AB1234567" in summary.text
    assert "Georgia" in summary.text


def test_user_edits_ocr_identification_number_value_persists(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            passport_number="OCR000000",
        ),
    )
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(identification_number="USER999999"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "USER999999" in summary.text
    assert "OCR000000" not in summary.text


def test_user_edits_ocr_citizenship_value_persists(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            citizenship="Georgia",
        ),
    )
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(citizenship="Armenia"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "Armenia" in summary.text
    assert "Georgia" not in summary.text


def test_manual_flow_without_ocr_still_works(fake_provider):
    """Baseline: a manual (no-OCR) order must still be creatable with the
    new identity fields left blank -- section 1's "existing manual flow ...
    должны работать как раньше" guarantee."""
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post("/policyholder", data=_minimal_policyholder_data(), follow_redirects=False)
    assert response.status_code == 303


def test_user_can_edit_the_ocr_prefilled_value(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            policyholder_full_name="Petrov Petr",
        ),
    )
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(full_name="Sidorov Ivan"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "Sidorov Ivan" in summary.text
    assert "Petrov Petr" not in summary.text


def test_validation_rerender_keeps_users_edit_not_the_ocr_value(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            policyholder_full_name="Petrov Petr",
        ),
    )
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(full_name="Sidorov Ivan", contact_email="not-an-email"),
    )
    assert response.status_code == 422
    assert 'value="Sidorov Ivan"' in response.text
    assert "Petrov Petr" not in response.text


def test_edit_policyholder_shows_saved_value_not_ocr(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            policyholder_full_name="Petrov Petr",
        ),
    )
    response = client_.post("/policyholder", data=_minimal_policyholder_data(full_name="Petrov Petr"), follow_redirects=False)
    resume_token = response.headers["location"].split("/")[2]

    edit = client_.get(f"/o/{resume_token}/edit-policyholder")
    assert edit.status_code == 200
    assert 'value="Petrov Petr"' in edit.text


# --------------------------- Driver block ---------------------------------------


def test_driver_defaults_to_same_as_policyholder(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'name="driver_same_as_policyholder" value="yes" checked' in response.text
    assert 'id="driver-fields" hidden' in response.text


def test_driver_yes_ignores_any_submitted_driver_fields(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(driver_same_as_policyholder="yes", driver_full_name="Should Be Ignored"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "Should Be Ignored" not in summary.text


def test_driver_no_requires_and_saves_driver_fields(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            driver_same_as_policyholder="no",
            driver_full_name="Sidorov Petr",
            driver_identifier="ID998877",
            driver_citizenship="Armenia",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "Sidorov Petr" in summary.text


def test_driver_no_missing_name_is_rejected(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(driver_same_as_policyholder="no"),
    )
    assert response.status_code == 422
    assert "driver_full_name" in response.text or "Водитель" in response.text


def test_ocr_driver_hint_never_leaks_policyholder_passport_or_citizenship(fake_provider):
    """Section 3: driver.identifier/citizenship must stay empty even when
    OCR found a policyholder passport_number/citizenship in the same
    document batch -- only driver_full_name is ever OCR-sourced for the
    driver block (see app.web.checkout_routes.get_policyholder, which
    builds `driver=_driver_context(same_as=True, full_name=...)` with no
    identifier/citizenship argument at all)."""
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            passport_number="AB1234567",
            citizenship="Georgia",
            driver_full_name="Petrov Petr",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'name="driver_full_name" value="Petrov Petr"' in response.text
    assert 'name="driver_identifier" value=""' in response.text
    assert 'name="driver_citizenship" value=""' in response.text


def test_ocr_driver_full_name_autofills_only_the_field_not_the_toggle(fake_provider):
    """Section 2.1: OCR-filled but only used once the user has already
    chosen Driver=Нет -- the toggle itself always defaults to Да."""
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            driver_full_name="Petrov Petr",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'name="driver_same_as_policyholder" value="yes" checked' in response.text  # still defaults to Да
    assert 'name="driver_full_name" value="Petrov Petr"' in response.text  # pre-filled, ready if user picks Нет


def test_driver_fields_restored_on_edit(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            driver_same_as_policyholder="no", driver_full_name="Sidorov Petr", driver_identifier="ID998877", driver_citizenship="Armenia"
        ),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]

    edit = client_.get(f"/o/{resume_token}/edit-policyholder")
    assert edit.status_code == 200
    assert 'name="driver_same_as_policyholder" value="no" checked' in edit.text
    assert 'value="Sidorov Petr"' in edit.text
    assert 'value="ID998877"' in edit.text


# --------------------------- Owner block ----------------------------------------


def test_owner_defaults_to_same_as_policyholder(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'name="owner_same_as_policyholder" value="yes" checked' in response.text
    assert 'id="owner-fields" hidden' in response.text


def test_owner_yes_ignores_any_submitted_owner_fields(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(owner_same_as_policyholder="yes", owner_full_name="Should Be Ignored"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "Should Be Ignored" not in summary.text


def test_owner_no_individual_requires_and_saves_fields(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            owner_same_as_policyholder="no",
            owner_entity_type="individual",
            owner_full_name="Sidorova Anna",
            owner_identifier="ID111222",
            owner_citizenship="Kazakhstan",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "Sidorova Anna" in summary.text


def test_owner_no_legal_requires_company_fields_not_citizenship(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            owner_same_as_policyholder="no",
            owner_entity_type="legal",
            owner_full_name='OOO "Romashka"',
            owner_identifier="LEGALCODE123",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]
    summary = client_.get(f"/o/{resume_token}/summary")
    assert "Romashka" in summary.text


def test_owner_no_legal_missing_company_name_is_rejected(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(owner_same_as_policyholder="no", owner_entity_type="legal"),
    )
    assert response.status_code == 422


def test_owner_no_individual_missing_citizenship_is_rejected(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            owner_same_as_policyholder="no", owner_entity_type="individual", owner_full_name="Sidorova Anna", owner_identifier="ID111222"
        ),
    )
    assert response.status_code == 422


def test_owner_yes_hides_the_entity_toggle_too(fake_provider):
    """Section 6: Owner=Да must hide EVERYTHING dependent on it, including
    the individual/legal entity toggle -- not just the individual/legal
    fields. #owner-entity-toggle lives nested inside #owner-fields, so it
    disappears together with it under the same `hidden` attribute; this
    checks that nesting (rather than a second, independently-hidden toggle)
    is really what's rendered."""
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.get("/policyholder")
    assert response.status_code == 200
    owner_fields_pos = response.text.index('id="owner-fields" hidden')
    entity_toggle_pos = response.text.index('id="owner-entity-toggle"')
    assert owner_fields_pos < entity_toggle_pos  # entity toggle opens after (nested inside) the hidden container


def test_owner_no_legal_hides_citizenship_field_and_relabels(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            owner_same_as_policyholder="no",
            owner_entity_type="legal",
            owner_full_name='OOO "Romashka"',
            owner_identifier="LEGALCODE123",
        ),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]
    edit = client_.get(f"/o/{resume_token}/edit-policyholder")
    assert edit.status_code == 200
    assert 'id="owner-citizenship-field" hidden' in edit.text
    assert "Название организации" in edit.text
    assert "Идентификационный код" in edit.text


def test_owner_no_individual_shows_citizenship_field_and_relabels(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            owner_same_as_policyholder="no",
            owner_entity_type="individual",
            owner_full_name="Sidorova Anna",
            owner_identifier="ID111222",
            owner_citizenship="Kazakhstan",
        ),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]
    edit = client_.get(f"/o/{resume_token}/edit-policyholder")
    assert edit.status_code == 200
    assert 'id="owner-citizenship-field" ' in edit.text and 'id="owner-citizenship-field" hidden' not in edit.text
    assert "Владелец" in edit.text
    assert "Идентификационный номер" in edit.text


def test_ocr_owner_hint_never_leaks_policyholder_passport_or_citizenship(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            passport_number="AB1234567",
            citizenship="Georgia",
            owner_full_name="Kowalski Jan",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'name="owner_full_name" value="Kowalski Jan"' in response.text
    assert 'name="owner_identifier" value=""' in response.text
    assert 'name="owner_citizenship" value=""' in response.text


def test_ocr_owner_full_name_autofills_only_the_field_not_the_toggle(fake_provider):
    client_ = TestClient(app)
    _reach_policyholder_via_ocr(
        client_,
        fake_provider,
        OcrResult(
            provider="fake",
            registration_number="DO002BB",
            vin="JYARJ41E7KA000901",
            chassis_number=None,
            manufacturer="ZDRIVEROWNERFICTIONALMAKE",
            model="ZDRIVEROWNERFICTIONALMODEL",
            owner_full_name="Kowalski Jan",
        ),
    )
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert 'name="owner_same_as_policyholder" value="yes" checked' in response.text  # still defaults to Да
    assert 'name="owner_full_name" value="Kowalski Jan"' in response.text


def test_owner_fields_restored_on_edit(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            owner_same_as_policyholder="no",
            owner_entity_type="legal",
            owner_full_name='OOO "Romashka"',
            owner_identifier="LEGALCODE123",
        ),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]

    edit = client_.get(f"/o/{resume_token}/edit-policyholder")
    assert edit.status_code == 200
    assert 'name="owner_same_as_policyholder" value="no" checked' in edit.text
    assert 'name="owner_entity_type" value="legal" checked' in edit.text
    assert "Romashka" in edit.text


# --------------------------- legacy orders ---------------------------------------


def test_legacy_order_without_driver_owner_data_renders_fine(fake_provider):
    """A pre-existing order (created before this migration) has no
    driver/owner columns populated -- DB defaults them to
    same_as_policyholder=True, which must render exactly like a fresh
    order that explicitly chose "Да", never a broken/blank state."""
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post("/policyholder", data=_minimal_policyholder_data(), follow_redirects=False)
    resume_token = response.headers["location"].split("/")[2]

    edit = client_.get(f"/o/{resume_token}/edit-policyholder")
    assert edit.status_code == 200
    assert 'name="driver_same_as_policyholder" value="yes" checked' in edit.text
    assert 'name="owner_same_as_policyholder" value="yes" checked' in edit.text


def test_legacy_order_missing_identification_and_citizenship_requires_them_before_resave(fake_provider):
    """Simulates a genuinely pre-existing order (identification_number/
    citizenship NULL in the DB, as they'd be for any order created before
    those columns existed) -- editing it must show empty fields, never
    crash, and require both before the edit can be saved."""
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post("/policyholder", data=_minimal_policyholder_data(), follow_redirects=False)
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        conn.execute(
            "UPDATE insurance_orders SET identification_number = NULL, citizenship = NULL WHERE resume_token = ?",
            (resume_token,),
        )
        conn.commit()
    finally:
        conn.close()

    edit = client_.get(f"/o/{resume_token}/edit-policyholder")
    assert edit.status_code == 200
    assert 'name="identification_number" value=""' in edit.text
    assert "selected>" not in edit.text  # no country pre-selected

    summary = client_.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200  # NULLs don't crash the summary render either

    resave_missing = client_.post(
        f"/o/{resume_token}/edit-policyholder",
        data={"full_name": "Ivanov Ivan", "contact_email": "ivan@example.com"},
    )
    assert resave_missing.status_code == 422

    resave_ok = client_.post(
        f"/o/{resume_token}/edit-policyholder",
        data=_minimal_policyholder_data(),
        follow_redirects=False,
    )
    assert resave_ok.status_code == 303


# --------------------------- Latin-only name validation --------------------------


def test_full_name_hyphen_and_apostrophe_accepted(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(full_name="Anne-Marie O'Brien"),
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_cyrillic_full_name_is_rejected(fake_provider):
    """Final chosen production behavior: tpl.ge's FAQ requires Latin
    script for all required/additional personal fields -- a Cyrillic name
    is rejected outright, never silently transliterated (see
    app.validation.validate_full_name)."""
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post("/policyholder", data=_minimal_policyholder_data(full_name="Иванов Иван"))
    assert response.status_code == 422
    assert "латинские" in response.text


def test_driver_full_name_cyrillic_is_rejected(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            driver_same_as_policyholder="no",
            driver_full_name="Сидоров Пётр",
            driver_identifier="ID998877",
            driver_citizenship="Armenia",
        ),
    )
    assert response.status_code == 422


def test_owner_full_name_cyrillic_is_rejected(fake_provider):
    client_ = TestClient(app)
    _reach_vehicle_manual(client_)
    response = client_.post(
        "/policyholder",
        data=_minimal_policyholder_data(
            owner_same_as_policyholder="no",
            owner_entity_type="individual",
            owner_full_name="Сидорова Анна",
            owner_identifier="ID111222",
            owner_citizenship="Kazakhstan",
        ),
    )
    assert response.status_code == 422
