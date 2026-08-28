"""Step 1 of the GE/AM/TR rollout: country-aware routing/draft/Order.

Covers only the plumbing -- /start accepting and validating a country,
country_code surviving the draft through every checkout step, and Order
creation reading it from the draft instead of a hardcoded constant. AM/TR
have no priced periods yet (deliberately, out of scope for this step -- see
app.web.checkout_routes module docstring), so an AM/TR draft cannot reach
/policyholder through the real /category-period route; the one test that
needs a real Order for a non-GE country seeds the rest of the draft
directly (see _seed_full_draft) to isolate "does country_code reach the
Order" from "does AM/TR pricing work" (the latter is explicitly not this
step's job).
"""

from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.db import get_connection
from app.deps import SESSION_COOKIE_NAME, get_settings
from app.main import app
from app.orders.repository import get_order_by_token
from app.sessions.repository import get_draft, merge_draft
from app.web.checkout_routes import DEFAULT_COUNTRY_CODE, SUPPORTED_COUNTRY_CODES, _draft_country_code
from policyholder_helpers import valid_policyholder_data

_settings = get_settings()

# Own catalog rows, independent of tests/test_routes_smoke.py's -- this file
# may be collected before or after it, so external_id=7/code="passenger_car"
# is deliberately the SAME external_id that module uses (ON CONFLICT(external_id)
# means whichever module runs first creates the row, the other just updates
# the same one). Manufacturer/model use external_id=15001 -- next free block
# after test_ocr_flow(5001)/motorcycle(6001-6002)/trailer(7001)/bus(8001)/
# truck(9001)/special_vehicle(10001)/date_validation(11001)/
# policyholder_contacts(12001)/payment(13001)/policyholder_driver_owner+
# telegram_notifications(14001) -- see each of those files' own comments for
# why upsert_manufacturer's global external_id conflict target means every
# test module sharing this DB file must claim its own unused block.
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=15001, name="ZCOUNTRYFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=15001, manufacturer_id=_manufacturer_id, name="ZCOUNTRYFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()


def _session_id(client: TestClient) -> str:
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id, "expected the session cookie to be set by now"
    return session_id


def _read_draft(client: TestClient) -> dict:
    conn = get_connection(_settings.app.db_file)
    try:
        return get_draft(conn, _session_id(client)) or {}
    finally:
        conn.close()


def _seed_full_draft(client: TestClient, *, country_code: str) -> None:
    """Fills in every draft key /policyholder needs, bypassing /category-period
    (which correctly rejects AM/TR today -- no priced periods exist for them
    yet). Values are otherwise arbitrary/realistic placeholders; this test
    is only about country_code, not pricing."""
    conn = get_connection(_settings.app.db_file)
    try:
        merge_draft(
            conn,
            _session_id(client),
            {
                "country_code": country_code,
                "vehicle_category_code": "passenger_car",
                "period_code": "15d",
                "price_customer_minor": 150000,
                "start_date": "2031-08-15",
                "end_date": "2031-08-30",
                "data_entry_method": "manual",
                "registration_number": "A123AA777",
                "identifier_type": "vin",
                "identifier": "JT123456789012345",
                "manufacturer_id": _manufacturer_id,
                "model_id": _model_id,
            },
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _draft_country_code unit tests
# ---------------------------------------------------------------------------


def test_draft_country_code_defaults_when_draft_is_none():
    assert _draft_country_code(None) == DEFAULT_COUNTRY_CODE


def test_draft_country_code_defaults_when_key_missing():
    assert _draft_country_code({}) == DEFAULT_COUNTRY_CODE


def test_draft_country_code_defaults_on_unknown_value():
    assert _draft_country_code({"country_code": "XX"}) == DEFAULT_COUNTRY_CODE


def test_draft_country_code_passes_through_supported_values():
    for code in SUPPORTED_COUNTRY_CODES:
        assert _draft_country_code({"country_code": code}) == code


# ---------------------------------------------------------------------------
# /start entry point
# ---------------------------------------------------------------------------


def test_start_without_country_defaults_to_ge():
    client = TestClient(app)
    response = client.get("/start", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/category-period"
    assert _read_draft(client)["country_code"] == "GE"


def test_start_with_country_ge():
    client = TestClient(app)
    client.get("/start", params={"country": "GE"}, follow_redirects=False)
    assert _read_draft(client)["country_code"] == "GE"


def test_start_with_country_am():
    client = TestClient(app)
    client.get("/start", params={"country": "AM"}, follow_redirects=False)
    assert _read_draft(client)["country_code"] == "AM"


def test_start_with_country_tr():
    client = TestClient(app)
    client.get("/start", params={"country": "TR"}, follow_redirects=False)
    assert _read_draft(client)["country_code"] == "TR"


def test_start_with_country_is_case_insensitive():
    client = TestClient(app)
    client.get("/start", params={"country": "am"}, follow_redirects=False)
    assert _read_draft(client)["country_code"] == "AM"


def test_start_with_invalid_country_falls_back_to_ge_not_saved_verbatim():
    client = TestClient(app)
    response = client.get("/start", params={"country": "XX"}, follow_redirects=False)
    assert response.status_code == 303
    draft = _read_draft(client)
    assert draft["country_code"] == "GE"
    assert draft["country_code"] != "XX"


# ---------------------------------------------------------------------------
# country_code survives the whole pre-order wizard
# ---------------------------------------------------------------------------


def test_country_code_persists_through_category_period_step():
    """AM has no priced periods yet (out of scope for this step), so
    /category-period must still render (never 500) and must not wipe the
    country_code merge_draft already wrote at /start."""
    client = TestClient(app)
    client.get("/start", params={"country": "AM"}, follow_redirects=False)

    response = client.get("/category-period")
    assert response.status_code == 200

    assert _read_draft(client)["country_code"] == "AM"


def test_country_code_persists_through_the_full_ge_wizard():
    """Drives the real GE happy path (the only one with priced periods right
    now) all the way to policyholder, checking country_code is still "GE"
    in the draft after every intermediate step's merge_draft call."""
    client = TestClient(app)
    client.get("/start", params={"country": "GE"}, follow_redirects=False)

    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    assert _read_draft(client)["country_code"] == "GE"

    client.post("/date", data={"start_date": "2031-08-15"})
    assert _read_draft(client)["country_code"] == "GE"

    client.post("/method", data={"choice": "manual"})
    assert _read_draft(client)["country_code"] == "GE"

    client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )
    assert _read_draft(client)["country_code"] == "GE"


# ---------------------------------------------------------------------------
# Order creation reads country_code from the draft
# ---------------------------------------------------------------------------


def test_order_created_from_ge_draft_has_country_code_ge():
    client = TestClient(app)
    client.get("/start", params={"country": "GE"}, follow_redirects=False)
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": "2031-08-15"})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )
    response = client.post("/policyholder", data=valid_policyholder_data(contact_telegram="@ge_test"), follow_redirects=False)
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "GE"


def test_order_created_from_am_draft_has_country_code_am():
    """AM pricing/periods don't exist yet, so the draft is seeded directly
    (see _seed_full_draft) rather than driven through /category-period --
    this isolates the one thing Step 1 is actually responsible for
    (country_code draft -> Order) from AM pricing, which is explicitly out
    of scope."""
    client = TestClient(app)
    client.get("/start", params={"country": "AM"}, follow_redirects=False)
    _seed_full_draft(client, country_code="AM")

    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_test"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "AM"


def test_order_created_from_tr_draft_has_country_code_tr():
    client = TestClient(app)
    client.get("/start", params={"country": "TR"}, follow_redirects=False)
    _seed_full_draft(client, country_code="TR")

    # TR requires date_of_birth at /policyholder as of Step 4 (see
    # tests/test_country_fields.py for full coverage of that requirement) --
    # supplied directly here since this test's own job is only to prove
    # country_code reaches the Order, not to re-test field requirements.
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_test", date_of_birth="1990-05-20"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "TR"


# ---------------------------------------------------------------------------
# Georgia backward compatibility
# ---------------------------------------------------------------------------


def test_bare_start_with_no_query_string_still_reaches_payment_as_ge():
    """The exact scenario the task calls out: an old bookmark/link to a bare
    /start (no ?country=...) must keep behaving exactly as before this
    change -- full happy path to the payment screen, order country_code GE."""
    client = TestClient(app)
    response = client.get("/start", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/category-period"

    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    client.post("/date", data={"start_date": "2031-08-15"})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
        },
    )
    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@backcompat"), follow_redirects=False
    )
    assert response.status_code == 303
    resume_token = response.headers["location"].split("/")[2]

    response = client.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.country_code == "GE"


def test_direct_category_period_post_without_ever_visiting_start_still_defaults_to_ge():
    """Covers sessions/tests that POST straight to /category-period without
    ever calling /start at all (e.g. every pre-existing test in
    tests/test_routes_smoke.py). Such a draft has no country_code key at
    all -- /start is the only writer of it -- so it must still *resolve* to
    GE via _draft_country_code's fallback, the same default every other
    step applies. (It's never written back into the draft just because it
    was read once -- see _draft_country_code's docstring.)"""
    client = TestClient(app)
    response = client.post(
        "/category-period", data={"category_code": "passenger_car", "period_code": "15d"}, follow_redirects=False
    )
    assert response.status_code == 303
    draft = _read_draft(client)
    assert "country_code" not in draft
    assert _draft_country_code(draft) == "GE"


# ---------------------------------------------------------------------------
# Homepage: unchanged in this step (see report -- AM/TR still route to the
# operator, not to a checkout with no priced periods).
# ---------------------------------------------------------------------------


def test_landing_page_still_links_plainly_to_start_for_georgia():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/start"' in response.text
    assert "country=AM" not in response.text
    assert "country=TR" not in response.text


def test_landing_page_armenia_and_turkey_still_route_to_the_operator():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.text.count("Оформить через оператора") == 2
