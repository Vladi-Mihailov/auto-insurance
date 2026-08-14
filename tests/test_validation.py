from app.validation import (
    validate_contacts_form,
    validate_email,
    validate_full_name,
    validate_identifier,
    validate_optional_phone,
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


def test_validate_email_accepts_valid_address():
    value, error = validate_email("ivan@example.com")
    assert error is None
    assert value == "ivan@example.com"


def test_validate_email_rejects_empty():
    value, error = validate_email("")
    assert value is None
    assert error is not None


def test_validate_email_rejects_malformed_address():
    value, error = validate_email("not-an-email")
    assert value is None
    assert error is not None


def test_validate_optional_phone_accepts_valid_format():
    value, error = validate_optional_phone("+995 555 12 34 56")
    assert error is None
    assert value == "+995 555 12 34 56"


def test_validate_optional_phone_empty_is_success_not_error():
    """Phone is optional -- unlike email, blank must not be an error."""
    value, error = validate_optional_phone("")
    assert value is None
    assert error is None


def test_validate_optional_phone_rejects_malformed_value_when_provided():
    value, error = validate_optional_phone("not a phone number!!")
    assert value is None
    assert error is not None


def test_validate_contacts_form_requires_only_email():
    clean, errors = validate_contacts_form(
        {
            "contact_email": "ivan@example.com",
            "contact_telegram": "",
            "contact_phone": "",
            "contact_max": "",
            "contact_other": "",
        }
    )
    assert errors == {}
    assert clean["contact_email"] == "ivan@example.com"
    assert clean["contact_telegram"] is None
    assert clean["contact_phone"] is None
    assert clean["contact_max"] is None
    assert clean["contact_other"] is None


def test_validate_contacts_form_missing_email_is_the_only_error():
    clean, errors = validate_contacts_form(
        {
            "contact_email": "",
            "contact_telegram": "@ivan",
            "contact_phone": "",
            "contact_max": "",
            "contact_other": "",
        }
    )
    assert clean is None
    assert list(errors.keys()) == ["contact_email"]


def test_validate_contacts_form_multiple_optional_contacts_can_coexist():
    """Independent fields, not a radio/exclusive choice -- several optional
    contacts filled in at once must all validate and all be kept."""
    clean, errors = validate_contacts_form(
        {
            "contact_email": "ivan@example.com",
            "contact_telegram": "@ivan",
            "contact_phone": "+995 555 12 34 56",
            "contact_max": "@ivan_max",
            "contact_other": "WhatsApp +7 900 000 00 00",
        }
    )
    assert errors == {}
    assert clean == {
        "contact_email": "ivan@example.com",
        "contact_telegram": "@ivan",
        "contact_phone": "+995 555 12 34 56",
        "contact_max": "@ivan_max",
        "contact_other": "WhatsApp +7 900 000 00 00",
    }
