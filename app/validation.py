"""Field validation for the checkout's manual-entry steps.

Deliberately permissive: cars in this product are registered in different
countries, so we do not assume a Russian/Georgian plate format, and chassis
numbers have no confirmed format at all yet. Validators return
(normalized_value, error_message) — error_message is None on success.
"""

import re

CONTACT_TYPES = ("telegram", "max", "phone", "other")
IDENTIFIER_TYPES = ("vin", "chassis")


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


def validate_contact(contact_type: str, raw_value: str) -> tuple[str | None, str | None]:
    if contact_type not in CONTACT_TYPES:
        return None, "Выберите способ связи"

    value = _collapse_spaces(raw_value)
    if not value:
        return None, "Укажите контакт"
    if len(value) > 120:
        return None, "Контакт: слишком длинное значение"

    if contact_type == "phone":
        if not re.match(r"^\+?[0-9\-\s()]{6,20}$", value):
            return None, "Телефон: укажите номер в формате +995XXXXXXXXX"

    return value, None


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
