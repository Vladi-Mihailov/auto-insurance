from app.validation import (
    validate_contact,
    validate_full_name,
    validate_identifier,
    validate_registration_number,
    validate_vehicle_details_form,
)


def test_identifier_normalizes_and_uppercases_vin():
    value, error = validate_identifier(" jt123456789012345 ", "vin")
    assert error is None
    assert value == "JT123456789012345"


def test_identifier_rejects_special_characters():
    value, error = validate_identifier("JT12-3456!", "vin")
    assert value is None
    assert error is not None


def test_identifier_rejects_empty():
    value, error = validate_identifier("", "vin")
    assert value is None
    assert error is not None


def test_identifier_accepts_chassis_the_same_lenient_way_as_vin():
    value, error = validate_identifier("chs12345", "chassis")
    assert error is None
    assert value == "CHS12345"


def test_registration_number_allows_foreign_plates():
    value, error = validate_registration_number("ab-123-cd")
    assert error is None
    assert value == "AB-123-CD"


def test_registration_number_rejects_too_long():
    value, error = validate_registration_number("A" * 20)
    assert value is None
    assert error is not None


def test_full_name_rejects_digits():
    value, error = validate_full_name("Ivanov Ivan 2")
    assert value is None
    assert error is not None


def test_full_name_accepts_hyphenated_name():
    value, error = validate_full_name("Anne-Marie O'Neil")
    assert error is None
    assert value == "Anne-Marie O'Neil"


def test_validate_vehicle_details_form_collects_all_errors():
    clean, errors = validate_vehicle_details_form(
        {
            "registration_number": "",
            "identifier_type": "vin",
            "identifier": "!!!",
        }
    )
    assert clean is None
    assert "registration_number" in errors
    assert "identifier" in errors


def test_validate_vehicle_details_form_success():
    clean, errors = validate_vehicle_details_form(
        {
            "registration_number": "a123aa777",
            "identifier_type": "vin",
            "identifier": "jt123456789012345",
        }
    )
    assert errors == {}
    assert clean["registration_number"] == "A123AA777"
    assert clean["identifier"] == "JT123456789012345"
    assert clean["identifier_type"] == "vin"


def test_validate_vehicle_details_form_rejects_unknown_identifier_type():
    clean, errors = validate_vehicle_details_form(
        {
            "registration_number": "A123AA777",
            "identifier_type": "passport",
            "identifier": "JT123456789012345",
        }
    )
    assert clean is None
    assert "identifier_type" in errors


def test_validate_contact_phone_format():
    value, error = validate_contact("phone", "+995 555 12 34 56")
    assert error is None
    assert value == "+995 555 12 34 56"


def test_validate_contact_unknown_type():
    value, error = validate_contact("carrier-pigeon", "x")
    assert value is None
    assert error is not None
