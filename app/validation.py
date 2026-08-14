"""Field validation for the checkout's manual-entry steps.

Deliberately permissive: cars in this product are registered in different
countries, so we do not assume a Russian/Georgian plate format, and chassis
numbers have no confirmed format at all yet. Validators return
(normalized_value, error_message) — error_message is None on success.
"""

import re

IDENTIFIER_TYPES = ("vin", "chassis")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def validate_required_text(raw: str, *, field_label: str, max_length: int = 120) -> tuple[str | None, str | None]:
    value = _collapse_spaces(raw)
    if not value:
        return None, f"{field_label}: заполните это поле"
    if len(value) > max_length:
        return None, f"{field_label}: слишком длинное значение"
    return value, None


def validate_registration_number(raw: str) -> tuple[str | None, str | None]:
    value = _collapse_spaces(raw).upper()
    if not value:
        return None, "Регистрационный номер: заполните это поле"
    if not (2 <= len(value) <= 15):
        return None, "Регистрационный номер: длина должна быть от 2 до 15 символов"
    if not all(ch.isalnum() or ch in " -" for ch in value):
        return None, "Регистрационный номер: разрешены только буквы, цифры, пробел и дефис"
    return value, None


def validate_identifier(raw: str, identifier_type: str) -> tuple[str | None, str | None]:
    """VIN or chassis number — same lenient rule for both until a real format
    is confirmed (see app/dates/rules.py-style TODO precedent): reject only
    obviously-wrong input (empty, too short/long, non-alphanumeric), not a
    strict 17-character VIN pattern that would block real foreign vehicles.
    """
    label = "VIN" if identifier_type == "vin" else "Номер шасси"
    value = re.sub(r"\s+", "", raw or "").upper()
    if not value:
        return None, f"{label}: заполните это поле"
    if not (3 <= len(value) <= 25):
        return None, f"{label}: длина должна быть от 3 до 25 символов"
    if not value.isalnum():
        return None, f"{label}: только латинские буквы и цифры, без пробелов и спецсимволов"
    return value, None


def validate_full_name(raw: str) -> tuple[str | None, str | None]:
    value = _collapse_spaces(raw)
    if not value:
        return None, "ФИО: заполните это поле"
    if len(value) < 3 or len(value) > 120:
        return None, "ФИО: длина должна быть от 3 до 120 символов"
    if not all(ch.isalpha() or ch in " -'" for ch in value):
        return None, "ФИО: разрешены только буквы, пробел, дефис и апостроф"
    return value, None


def validate_email(raw: str) -> tuple[str | None, str | None]:
    value = _collapse_spaces(raw)
    if not value:
        return None, "Email: заполните это поле"
    if len(value) > 120:
        return None, "Email: слишком длинное значение"
    if not _EMAIL_RE.match(value):
        return None, "Email: укажите корректный адрес"
    return value, None


def validate_optional_contact_text(raw: str, *, field_label: str, max_length: int = 120) -> tuple[str | None, str | None]:
    """Telegram/MAX/"other" contact fields — optional, so an empty value is
    success (None, None), not an error. Only checked when actually filled in."""
    value = _collapse_spaces(raw)
    if not value:
        return None, None
    if len(value) > max_length:
        return None, f"{field_label}: слишком длинное значение"
    return value, None


def validate_optional_phone(raw: str) -> tuple[str | None, str | None]:
    value = _collapse_spaces(raw)
    if not value:
        return None, None
    if len(value) > 120:
        return None, "Телефон: слишком длинное значение"
    if not re.match(r"^\+?[0-9\-\s()]{6,20}$", value):
        return None, "Телефон: укажите номер в формате +995XXXXXXXXX"
    return value, None


def validate_contacts_form(form: dict) -> tuple[dict | None, dict[str, str]]:
    """Email is the only required contact; Telegram/phone/MAX/"other" are
    independent optional fields that may coexist (never mutually exclusive
    the way the old single contact_type/contact_value radio-select was).
    Returns (clean_data, errors); clean_data is None if there are any
    errors — same shape as validate_vehicle_details_form."""
    errors: dict[str, str] = {}
    clean: dict = {}

    email, err = validate_email(form.get("contact_email", ""))
    if err:
        errors["contact_email"] = err
    clean["contact_email"] = email

    telegram, err = validate_optional_contact_text(form.get("contact_telegram", ""), field_label="Telegram")
    if err:
        errors["contact_telegram"] = err
    clean["contact_telegram"] = telegram

    phone, err = validate_optional_phone(form.get("contact_phone", ""))
    if err:
        errors["contact_phone"] = err
    clean["contact_phone"] = phone

    max_contact, err = validate_optional_contact_text(form.get("contact_max", ""), field_label="MAX")
    if err:
        errors["contact_max"] = err
    clean["contact_max"] = max_contact

    other, err = validate_optional_contact_text(form.get("contact_other", ""), field_label="Другое")
    if err:
        errors["contact_other"] = err
    clean["contact_other"] = other

    if errors:
        return None, errors
    return clean, {}


def validate_vehicle_details_form(form: dict) -> tuple[dict | None, dict[str, str]]:
    """Validates the pure-field-shape part of the vehicle-details step
    (registration number + VIN/chassis toggle + value). manufacturer_id/
    model_id are validated separately against the catalog (needs a DB
    connection) — see app.catalog.repository — never trusted from the form
    alone. Returns (clean_data, errors); clean_data is None if there are
    any errors.
    """
    errors: dict[str, str] = {}
    clean: dict = {}

    registration_number, err = validate_registration_number(form.get("registration_number", ""))
    if err:
        errors["registration_number"] = err
    clean["registration_number"] = registration_number

    identifier_type = form.get("identifier_type", "")
    if identifier_type not in IDENTIFIER_TYPES:
        errors["identifier_type"] = "Выберите VIN или номер шасси"
        identifier_type = "vin"  # fall back only for the identifier validation call below
    clean["identifier_type"] = form.get("identifier_type") if form.get("identifier_type") in IDENTIFIER_TYPES else None

    identifier, err = validate_identifier(form.get("identifier", ""), identifier_type)
    if err:
        errors["identifier"] = err
    clean["identifier"] = identifier

    if errors:
        return None, errors
    return clean, {}
