"""Policyholder screen regression tests: the new multi-field contact block
(replacing the old single contact_type/contact_value radio-select), the
"ФИО (как в загранпаспорте)" label, and the manufacturer "Other" hint.

Required-field contract (full_name/identification_number/citizenship/
contact_email required, Telegram/phone/MAX/"other" optional) and Latin-only
name validation are covered in tests/test_policyholder_driver_owner.py,
alongside OCR autofill and the Driver/Owner blocks -- this file stays
scoped to the CONTACT block itself + the passport-style ФИО label, using
tests/policyholder_helpers.valid_policyholder_data() to satisfy the
required fields it isn't testing.

"passenger_car" is re-seeded with the same external_id=7 already used by
test_routes_smoke.py -- upsert_category is a true idempotent upsert, so
this is safe regardless of test collection order.
"""

from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import get_settings
from app.main import app
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
# external_id chosen to not collide with other test files' ranges.
_manufacturer_id = upsert_manufacturer(_conn, external_id=12001, name="ZCONTACTFICTIONALMAKE", is_popular=True)
_model_id = upsert_model(_conn, external_id=12001, manufacturer_id=_manufacturer_id, name="ZCONTACTFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


def _reach_policyholder(client_):
    client_.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client_.post("/date", data={"start_date": "2026-08-20"})
    client_.post("/method", data={"choice": "manual"})
    client_.post(
        "/vehicle",
        data={
            "registration_number": "PH001AA",
            "identifier_type": "vin",
            "identifier": "JYARJ41E7KA000300",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )


# --------------------------- name label / no hidden patronymic ---------------


def test_policyholder_label_uses_passport_style_wording():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.get("/policyholder")
    assert response.status_code == 200
    assert "ФИО (как в загранпаспорте)" in response.text


def test_full_name_accepts_latin_passport_style_spelling():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(full_name="Ivanov Ivan Ivanovich"),
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_full_name_two_words_no_patronymic_required():
    """No separate patronymic requirement exists anywhere in validation --
    a two-word name must be accepted just like a three-word one."""
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(full_name="Ivanov Ivan"),
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_old_contact_method_radio_block_is_gone():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.get("/policyholder")
    assert "Куда отправить готовый полис?" not in response.text
    assert 'name="contact_type"' not in response.text
    assert 'name="contact_value"' not in response.text


# --------------------------- contact validation -------------------------------


def test_email_is_required():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post("/policyholder", data=valid_policyholder_data(contact_email=""))
    assert response.status_code == 422
    assert "contact_email" in response.text or "Email" in response.text


def test_invalid_email_is_rejected():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post("/policyholder", data=valid_policyholder_data(contact_email="not-an-email"))
    assert response.status_code == 422


def test_valid_full_submission_is_accepted():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/summary")


def test_telegram_phone_max_other_are_all_optional():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(),
        follow_redirects=False,
    )
    assert response.status_code == 303  # no telegram/phone/max/other supplied at all


def test_multiple_optional_contacts_can_coexist_not_exclusive():
    """Independent fields, never a radio/exclusive choice."""
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(
            contact_telegram="@ivan",
            contact_phone="+995 555 12 34 56",
            contact_max="@ivan_max",
            contact_other="WhatsApp",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    summary = client_.get(f"/o/{resume_token}/summary")
    assert "ivan@example.com" in summary.text
    assert "@ivan" in summary.text
    assert "+995 555 12 34 56" in summary.text
    assert "@ivan_max" in summary.text
    assert "WhatsApp" in summary.text


def test_validation_failure_does_not_erase_already_entered_values():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(
            contact_email="not-an-email",
            contact_telegram="@ivan",
            contact_phone="+995 555 12 34 56",
        ),
    )
    assert response.status_code == 422
    assert 'value="not-an-email"' in response.text
    assert 'value="@ivan"' in response.text
    assert 'value="+995 555 12 34 56"' in response.text
    assert 'value="Ivanov Ivan"' in response.text
    assert 'value="AB1234567"' in response.text  # identification_number also survives
    assert '<option value="Georgia" selected>' in response.text  # citizenship also survives


def test_contacts_persist_through_edit_policyholder_resume():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@ivan"),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]

    edit = client_.get(f"/o/{resume_token}/edit-policyholder")
    assert edit.status_code == 200
    assert 'value="ivan@example.com"' in edit.text
    assert 'value="@ivan"' in edit.text


def test_edit_policyholder_can_add_and_update_contacts():
    client_ = TestClient(app)
    _reach_policyholder(client_)
    response = client_.post(
        "/policyholder",
        data=valid_policyholder_data(),
        follow_redirects=False,
    )
    resume_token = response.headers["location"].split("/")[2]

    updated = client_.post(
        f"/o/{resume_token}/edit-policyholder",
        data=valid_policyholder_data(contact_email="ivan-new@example.com", contact_phone="+995 555 99 88 77"),
        follow_redirects=False,
    )
    assert updated.status_code == 303

    summary = client_.get(f"/o/{resume_token}/summary")
    assert "ivan-new@example.com" in summary.text
    assert "+995 555 99 88 77" in summary.text


# --------------------------- manufacturer/model "Other" hint ------------------


def test_manufacturer_other_hint_matches_model_hint_style():
    """Section 2: "Other" is confirmed present in the real manufacturer
    catalog (external_id=1, synced from tpl.ge) as a regular selectable
    row, same as any model's "Other" row -- see app/catalog/client.py and
    the tpl.ge research findings. The hint is added only because this is
    factually true, not assumed."""
    client_ = TestClient(app)
    _reach_policyholder(client_)  # already POSTed /vehicle once; GET re-renders it filled in
    response = client_.get("/vehicle")
    assert response.status_code == 200
    assert "Если не удалось найти производителя — выберите „Other“." in response.text
    assert "Если не удалось найти модель — выберите „Other“." in response.text
