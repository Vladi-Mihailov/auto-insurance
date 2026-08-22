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
    """Latin-only, matching tpl.ge's own official FAQ requirement ("Обязательные
    и дополнительные поля должны быть заполнены... только латинскими
    символами") -- the same rule already asked of the model for OCR-read
    names (see app.ocr.provider's prompt: policyholder_full_name/
    driver_full_name/owner_full_name are always requested in Latin script).
    A Cyrillic (or any non-Latin) name is REJECTED here, never silently
    transliterated: guessing a different spelling of someone's own name is
    exactly the kind of guess this product avoids everywhere else (see the
    OCR prompt's own "не угадывай" rule) -- the person must retype it
    themselves. Shared by the policyholder's own ФИО and both driver/owner
    individual name fields (see validate_driver_form/validate_owner_form)."""
    value = _collapse_spaces(raw)
    if not value:
        return None, "ФИО: заполните это поле"
    if len(value) < 3 or len(value) > 120:
        return None, "ФИО: длина должна быть от 3 до 120 символов"
    if not all((ch.isascii() and ch.isalpha()) or ch in " -'" for ch in value):
        return None, "ФИО: используйте только латинские буквы, пробел, дефис и апостроф"
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


def validate_optional_email(raw: str) -> tuple[str | None, str | None]:
    """Same shape as validate_optional_phone -- empty is success (this
    field is optional), but a non-empty value must still look like a real
    email, same format check as the required contact_email."""
    value = _collapse_spaces(raw)
    if not value:
        return None, None
    if len(value) > 120:
        return None, "Email: слишком длинное значение"
    if not _EMAIL_RE.match(value):
        return None, "Email: укажите корректный адрес"
    return value, None


def validate_citizenship(raw: str) -> tuple[str | None, str | None]:
    """The policyholder's own citizenship field -- REQUIRED, matching
    tpl.ge's official FAQ (citizenship is one of the mandatory identity
    fields). A closed choice (a <select> populated from
    app.countries.COUNTRIES) -- rejecting anything not exactly one of
    those names is what keeps a form-tampered/stale submission from
    writing an invented country into the order (see app.countries's
    module docstring for why OCR itself already only ever proposes one of
    these same names or nothing at all)."""
    from app.countries import COUNTRIES  # local import: keep validation.py decoupled from the country list until needed

    value = _collapse_spaces(raw)
    if not value or value not in COUNTRIES:
        return None, "Гражданство: выберите страну из списка"
    return value, None


def validate_identification_number(raw: str, *, field_label: str) -> tuple[str | None, str | None]:
    """Personal ID / legal entity identification code -- REQUIRED. Shared by
    the policyholder's own "Идентификационный номер" and the driver/owner
    blocks (see app.validation.validate_driver_form/validate_owner_form).
    No confirmed universal format across countries, same lenient stance as
    validate_identifier (VIN/chassis) and validate_registration_number:
    reject only obviously-wrong input, never a strict country-specific
    pattern that would block a real foreign document. Deliberately NOT
    restricted to Latin script like validate_full_name -- this is a
    document identifier (digits/letters in whatever form the issuing
    country prints it), not a personal name tpl.ge's Latin-only FAQ rule
    was written for. OCR sometimes reads the "№" sign printed next to the
    number on a passport (e.g. "67№1647108"), which existing validation
    doesn't accept -- stripped here (along with any spaces right around it)
    rather than added to the allowed-character set, so stored values stay
    restricted to letters/digits/space/hyphen."""
    value = _collapse_spaces(re.sub(r"\s*№\s*", "", raw or "")).upper()
    if not value:
        return None, f"{field_label}: заполните это поле"
    if not (2 <= len(value) <= 30):
        return None, f"{field_label}: длина должна быть от 2 до 30 символов"
    if not all(ch.isalnum() or ch in " -" for ch in value):
        return None, f"{field_label}: разрешены только буквы, цифры, пробел и дефис"
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


def validate_driver_form(form: dict) -> tuple[dict | None, dict[str, str]]:
    """driver_same_as_policyholder ("yes"/"no") is the authoritative
    switch, matching tpl.ge's own semantics (see the OCR/checkout task
    report's tpl.ge research + its FAQ: driver info is additional personal
    data, only relevant once you've said the driver is a different
    person). "yes" (the default) means no driver fields are validated or
    saved at all, regardless of what a tampered/stale form submitted
    alongside it -- never partially trust fields that shouldn't apply.
    Returns (clean_data, errors); clean_data is None only when "no" was
    chosen and a sub-field failed validation (same shape as
    validate_vehicle_details_form/validate_contacts_form)."""
    same_as = form.get("driver_same_as_policyholder", "yes") != "no"
    if same_as:
        return {
            "driver_same_as_policyholder": True,
            "driver_full_name": None,
            "driver_identifier": None,
            "driver_citizenship": None,
            "driver_phone": None,
            "driver_email": None,
        }, {}

    errors: dict[str, str] = {}
    clean: dict = {"driver_same_as_policyholder": False}

    full_name, err = validate_full_name(form.get("driver_full_name", ""))
    if err:
        errors["driver_full_name"] = err
    clean["driver_full_name"] = full_name

    identifier, err = validate_identification_number(form.get("driver_identifier", ""), field_label="Идентификационный номер")
    if err:
        errors["driver_identifier"] = err
    clean["driver_identifier"] = identifier

    citizenship, err = validate_required_text(form.get("driver_citizenship", ""), field_label="Гражданство")
    if err:
        errors["driver_citizenship"] = err
    clean["driver_citizenship"] = citizenship

    phone, err = validate_optional_phone(form.get("driver_phone", ""))
    if err:
        errors["driver_phone"] = err
    clean["driver_phone"] = phone

    email, err = validate_optional_email(form.get("driver_email", ""))
    if err:
        errors["driver_email"] = err
    clean["driver_email"] = email

    if errors:
        return None, errors
    return clean, {}


_OWNER_ENTITY_TYPES = ("individual", "legal")


def validate_owner_form(form: dict) -> tuple[dict | None, dict[str, str]]:
    """owner_same_as_policyholder ("yes"/"no") mirrors validate_driver_form
    exactly -- "yes" (default) means no owner fields are validated/saved.
    When "no", owner_entity_type ("individual"/"legal", default
    "individual") additionally decides which fields apply: a legal entity
    has no citizenship, and owner_full_name/owner_identifier are relabeled
    (company name / identification code) but reuse the same two columns
    -- see app.orders.models.Order's owner_full_name/owner_identifier
    docstrings."""
    same_as = form.get("owner_same_as_policyholder", "yes") != "no"
    if same_as:
        return {
            "owner_same_as_policyholder": True,
            "owner_entity_type": None,
            "owner_full_name": None,
            "owner_identifier": None,
            "owner_citizenship": None,
            "owner_phone": None,
            "owner_email": None,
        }, {}

    entity_type = form.get("owner_entity_type", "individual")
    if entity_type not in _OWNER_ENTITY_TYPES:
        entity_type = "individual"

    errors: dict[str, str] = {}
    clean: dict = {"owner_same_as_policyholder": False, "owner_entity_type": entity_type}

    if entity_type == "legal":
        full_name, err = validate_required_text(form.get("owner_full_name", ""), field_label="Название организации", max_length=200)
    else:
        full_name, err = validate_full_name(form.get("owner_full_name", ""))
    if err:
        errors["owner_full_name"] = err
    clean["owner_full_name"] = full_name

    id_label = "Идентификационный код" if entity_type == "legal" else "Идентификационный номер"
    identifier, err = validate_identification_number(form.get("owner_identifier", ""), field_label=id_label)
    if err:
        errors["owner_identifier"] = err
    clean["owner_identifier"] = identifier

    if entity_type == "individual":
        citizenship, err = validate_required_text(form.get("owner_citizenship", ""), field_label="Гражданство")
        if err:
            errors["owner_citizenship"] = err
        clean["owner_citizenship"] = citizenship
    else:
        clean["owner_citizenship"] = None  # not applicable to legal entities

    phone, err = validate_optional_phone(form.get("owner_phone", ""))
    if err:
        errors["owner_phone"] = err
    clean["owner_phone"] = phone

    email, err = validate_optional_email(form.get("owner_email", ""))
    if err:
        errors["owner_email"] = err
    clean["owner_email"] = email

    if errors:
        return None, errors
    return clean, {}
