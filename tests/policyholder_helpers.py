"""Shared fictional-but-valid /policyholder form data for tests that just
need to get past this step to reach an order -- not testing policyholder
validation itself (see tests/test_policyholder_driver_owner.py for that).

Not a test module itself (no test_ prefix, so pytest never collects it).

Fictional Latin identity values only -- never real customer data, and
never a hidden PRODUCTION fallback (app.validation has no default of its
own for any of these; a submission missing them is always a validation
error, exactly as tpl.ge itself requires)."""


def valid_policyholder_data(**overrides) -> dict:
    data = {
        "full_name": "Ivanov Ivan",
        "identification_number": "AB1234567",
        "citizenship": "Georgia",
        "contact_email": "ivan@example.com",
    }
    data.update(overrides)
    return data
