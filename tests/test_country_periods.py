"""Step 3 of the GE/AM/TR rollout: country-aware period/date model.

GE: fixed periods, unchanged. TR: fixed periods (30/45/90/180/365d), real
AVAILABILITY but deliberately no price yet (see config.yaml's
pricing.TR.passenger_car -- price_rub: null everywhere), so TR cannot
complete a real /category-period submission through the UI either (blocked
on the existing "not priced yet" check, same as GE would be if a period
were ever unpriced). AM: EXACT DATE RANGE (10-365 days) -- no period_code,
no price at all yet (see app.web.checkout_routes.post_policyholder's price
guard) -- so AM ALSO cannot reach order creation in this step.

Because neither AM nor TR can complete a full order yet (by design -- see
the Step 3 report's price-blocker section), several tests here seed the
pre-order draft directly (via merge_draft) to exercise /date's date-rule
dispatch in isolation, the same technique tests/test_country_routing.py
already used to verify country_code reaches Order creation without needing
real AM/TR pricing.

All dates are computed relative to today_in_georgia() rather than hardcoded
literals -- this session's own established time-bomb lesson (see
tests/test_payment.py's stale "2026-08-20" baseline failures): a fixed
future date silently becomes a past one as real time advances.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog.repository import mark_models_synced, upsert_category, upsert_manufacturer, upsert_model
from app.dates.rules import today_in_georgia
from app.db import get_connection
from app.deps import SESSION_COOKIE_NAME, get_settings
from app.main import app
from app.sessions.repository import get_draft, merge_draft

_settings = get_settings()

# Own catalog rows -- external_id=16001 continues the per-file numbering
# convention documented in tests/test_country_routing.py (category
# external_id=7/"passenger_car" is the one deliberately-shared id every file
# reuses, idempotent via ON CONFLICT(external_id)).
_conn = get_connection(_settings.app.db_file)
upsert_category(_conn, external_id=7, code="passenger_car", name="Легковой", icon="vehicle")
_manufacturer_id = upsert_manufacturer(_conn, external_id=16001, name="ZPERIODSFICTIONALMAKE", is_popular=False)
_model_id = upsert_model(_conn, external_id=16001, manufacturer_id=_manufacturer_id, name="ZPERIODSFICTIONALMODEL")
mark_models_synced(_conn, _manufacturer_id)
_conn.commit()
_conn.close()

# A fixed reference point far enough in the future that it stays valid
# regardless of when this suite actually runs -- every date in this file is
# an offset from here, never an absolute calendar literal.
_START = today_in_georgia() + timedelta(days=60)


def _iso(offset_days: int) -> str:
    return (_START + timedelta(days=offset_days)).isoformat()


@pytest.fixture
def real_config(monkeypatch):
    """AM/TR's period/duration config lives in the REAL config/config.yaml,
    not the test fixture -- same fixture shape as
    tests/test_country_categories.py's real_config."""
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start(client: TestClient, country: str) -> None:
    client.get("/start", params={"country": country}, follow_redirects=False)


def _session_id(client: TestClient) -> str:
    session_id = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_id
    return session_id


def _read_draft(client: TestClient) -> dict:
    conn = get_connection(_settings.app.db_file)
    try:
        return get_draft(conn, _session_id(client)) or {}
    finally:
        conn.close()


def _seed_draft(client: TestClient, **updates) -> None:
    conn = get_connection(_settings.app.db_file)
    try:
        merge_draft(conn, _session_id(client), updates)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Georgia: unchanged (fixed period, existing date semantics)
# ---------------------------------------------------------------------------


def test_ge_15d_30d_90d_still_available_and_priced(real_config):
    client = TestClient(app)
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    codes = {p["code"]: p for p in response.json()}
    assert set(codes) == {"15d", "30d", "90d"}
    for code in ("15d", "30d", "90d"):
        assert codes[code]["is_priced"] is True


def test_ge_date_calculation_unchanged():
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})
    response = client.get("/api/date-preview", params={"start": _iso(0)})
    assert response.json() == {"end_date": _iso(15)}  # unchanged GeorgiaDateRule semantics


def test_ge_full_flow_still_reaches_date_step_with_computed_end_date():
    client = TestClient(app)
    _start(client, "GE")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    response = client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/method"
    draft = _read_draft(client)
    assert draft["period_code"] == "30d"
    assert draft["start_date"] == _iso(0)
    assert draft["end_date"] == _iso(30)


# ---------------------------------------------------------------------------
# Armenia: EXACT DATE RANGE (10-365 days), no fixed period
# ---------------------------------------------------------------------------


def test_am_category_period_screen_has_no_period_grid(real_config):
    client = TestClient(app)
    _start(client, "AM")
    response = client.get("/category-period")
    assert response.status_code == 200
    assert 'id="period-grid"' not in response.text
    assert "на следующем шаге" in response.text


def test_am_date_screen_shows_both_start_and_end_inputs(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.get("/date")
    assert response.status_code == 200
    assert 'name="start_date"' in response.text
    assert 'name="end_date"' in response.text  # editable, unlike GE/TR's read-only computed display


def test_am_10_days_accepted_and_period_code_stays_none(real_config):
    """The report's 10-day semantics example: start/end 10 days apart ->
    duration_days = (end - start).days = 10, accepted (matches the minimum
    exactly)."""
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(10)}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/method"
    draft = _read_draft(client)
    assert draft["start_date"] == _iso(0)
    assert draft["end_date"] == _iso(10)
    assert draft["period_code"] is None  # never a fake code like "10d"/"custom"/"dynamic"


def test_am_9_days_rejected(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(9)})
    assert response.status_code == 422
    assert "Минимальный срок страхования — 10 дней" in response.text


def test_am_11_days_accepted(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(11)}, follow_redirects=False)
    assert response.status_code == 303


def test_am_arbitrary_27_day_duration_accepted(real_config):
    """The whole point of EXACT DATE RANGE: 27 is not a period code anyone
    pre-defined, just a date-range difference."""
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(27)}, follow_redirects=False)
    assert response.status_code == 303
    draft = _read_draft(client)
    assert draft["start_date"] == _iso(0)
    assert draft["end_date"] == _iso(27)


def test_am_365_days_accepted(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(365)}, follow_redirects=False)
    assert response.status_code == 303


def test_am_366_days_rejected(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    response = client.post("/date", data={"start_date": _iso(0), "end_date": _iso(366)})
    assert response.status_code == 422
    assert "Максимальный срок страхования — 365 дней" in response.text


def test_am_start_date_in_the_past_still_rejected_same_as_ge(real_config):
    """AM start_date >= today uses the exact same
    _parse_and_validate_start_date GE already relies on -- no new rule."""
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    yesterday = (today_in_georgia() - timedelta(days=1)).isoformat()
    day_before = (today_in_georgia() - timedelta(days=2)).isoformat()
    response = client.post("/date", data={"start_date": day_before, "end_date": yesterday})
    assert response.status_code == 422
    assert "Дата начала не может быть раньше сегодняшнего дня" in response.text


def test_am_never_leaks_a_ge_period_code_into_the_draft(real_config):
    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "15d"})  # ignored input
    draft = _read_draft(client)
    assert draft["period_code"] is None
    assert draft.get("price_customer_minor") is None


# ---------------------------------------------------------------------------
# Turkey: FIXED period (30/45/90/180/365d), available but unpriced
# ---------------------------------------------------------------------------


def test_tr_periods_exactly_30_45_90_180_365(real_config):
    """Underlying AVAILABILITY (app.pricing.provider), not the
    customer-facing /api/periods endpoint -- see
    test_tr_api_periods_only_returns_priced_periods below for what the
    browser actually receives now that 30d has a confirmed price."""
    from app.pricing.provider import available_periods

    codes = [p.code for p in available_periods(get_settings(), "TR", "passenger_car")]
    assert codes == ["30d", "45d", "90d", "180d", "365d"]
    assert "15d" not in codes  # no GE period leaking in


def test_tr_api_periods_only_returns_priced_periods(real_config):
    """Customer-facing: 30d/45d/90d (priced) are offered -- 180d/365d
    stay configured (see the test above) but are never selectable."""
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    codes = [p["code"] for p in response.json()]
    assert codes == ["30d", "45d", "90d"]


def test_tr_selected_period_survives_into_date_step_via_seeded_draft(real_config):
    """45d is priced now (see test_tr_45_90_now_priced_180_365_remain_unselectable),
    but this still seeds the draft directly rather than going through
    /category-period, to isolate and verify the DATE RULE dispatch itself in
    its own right: /date must resolve TR's own FixedDurationDateRule, not
    silently fall back to GeorgiaDateRule (which doesn't even recognize
    "45d")."""
    client = TestClient(app)
    _start(client, "TR")
    _seed_draft(client, vehicle_category_code="passenger_car", period_code="45d")
    response = client.get("/date")
    assert response.status_code == 200
    response = client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)
    assert response.status_code == 303
    draft = _read_draft(client)
    assert draft["end_date"] == _iso(45)


def test_tr_end_date_calculated_correctly_for_every_period(real_config):
    expected_offsets = {"30d": 30, "45d": 45, "90d": 90, "180d": 180, "365d": 365}
    for period_code, offset_days in expected_offsets.items():
        client = TestClient(app)
        _start(client, "TR")
        _seed_draft(client, vehicle_category_code="passenger_car", period_code=period_code)
        response = client.get("/api/date-preview", params={"start": _iso(0)})
        assert response.status_code == 200, (period_code, response.text)
        assert response.json() == {"end_date": _iso(offset_days)}, period_code


def test_tr_future_start_date_is_not_artificially_blocked(real_config):
    """The unverified broker "today only" rule from the gap-analysis
    research must NOT have been copied into our own code -- a start_date
    200 days out is accepted, same as Georgia's own (which has no upper
    bound either)."""
    client = TestClient(app)
    _start(client, "TR")
    _seed_draft(client, vehicle_category_code="passenger_car", period_code="30d")
    response = client.post("/date", data={"start_date": _iso(200)}, follow_redirects=False)
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_invalid_period_post_cannot_inject_a_ge_period_for_am(real_config):
    client = TestClient(app)
    _start(client, "AM")
    response = client.post(
        "/category-period", data={"category_code": "passenger_car", "period_code": "15d"}, follow_redirects=False
    )
    assert response.status_code == 303  # category alone is complete for AM -- period_code is simply ignored
    draft = _read_draft(client)
    assert draft["period_code"] is None


def test_invalid_period_post_cannot_inject_a_ge_period_for_tr(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.post("/category-period", data={"category_code": "passenger_car", "period_code": "not-a-real-code"})
    assert response.status_code == 422
    assert "Выберите один из доступных периодов" in response.text


def test_am_order_creation_now_succeeds_with_real_linear_pricing(real_config):
    """AM passenger_car now has real linear pricing (see
    tests/test_am_linear_pricing.py for the formula's own dedicated
    coverage) -- the price guard in post_policyholder
    (draft.get("price_customer_minor") is None) simply stops firing for AM,
    exactly as documented there; it is not removed or weakened, it just has
    nothing left to block for this (country, category) pair. Drives the
    wizard all the way through /vehicle (real manufacturer/model) so
    post_policyholder's manufacturer/model guard doesn't intercept first."""
    from policyholder_helpers import valid_policyholder_data

    conn = get_connection(_settings.app.db_file)
    try:
        before_count = conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0]
    finally:
        conn.close()

    client = TestClient(app)
    _start(client, "AM")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": ""})
    client.post("/date", data={"start_date": _iso(0), "end_date": _iso(10)})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "A123AA777",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
            "engine_power": "150",  # required for AM as of Step 4
        },
    )

    response = client.post(
        "/policyholder", data=valid_policyholder_data(contact_telegram="@am_priced"), follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] != "/category-period"

    conn = get_connection(_settings.app.db_file)
    try:
        after_count = conn.execute("SELECT COUNT(*) FROM insurance_orders").fetchone()[0]
    finally:
        conn.close()
    assert after_count == before_count + 1  # order created, with a real price

    resume_token = response.headers["location"].split("/")[2]
    from app.orders.repository import get_order_by_token

    conn = get_connection(_settings.app.db_file)
    try:
        order = get_order_by_token(conn, resume_token)
    finally:
        conn.close()
    assert order.price_customer_minor == 108200  # 10d = 1082 RUB, see test_am_linear_pricing.py


# ---------------------------------------------------------------------------
# Future-proofing: resume/summary for a period_code=None order.
#
# No real AM order can exist via the checkout UI in this step (see the price
# guard above), but Order.period_code being NULL-able surfaced two latent
# bugs while investigating it -- app.web.routes._resume_redirect and
# get_summary both used to treat ANY period_code-less order as an ancient
# LEGACY one and bounce it to /o/{token}/period, which has no idea what an
# EXACT DATE RANGE product is. Both were fixed as part of this step (see the
# report). These tests build such an order directly via create_order() --
# bypassing the checkout routes' own price guard entirely, exactly as if
# AM pricing already existed -- purely to prove the fix isn't dead code.
# ---------------------------------------------------------------------------


def _create_am_order_directly(*, price_customer_minor: int) -> str:
    from app.orders.repository import create_order

    conn = get_connection(_settings.app.db_file)
    try:
        order = create_order(
            conn,
            session_id="am-future-proofing-test",
            country_code="AM",
            vehicle_category_code="passenger_car",
            period_code=None,
            start_date=_START,
            end_date=_START + timedelta(days=10),
            price_customer_minor=price_customer_minor,
            data_entry_method="manual",
            registration_number="AM123AA",
            identifier_type="vin",
            identifier="JT123456789012345",
            manufacturer_id=_manufacturer_id,
            manufacturer_name="ZPERIODSFICTIONALMAKE",
            model_id=_model_id,
            model_name="ZPERIODSFICTIONALMODEL",
            full_name="Ivanov Ivan",
            contact_email="ivan@example.com",
            contact_telegram=None,
            contact_phone=None,
            contact_max=None,
            contact_other=None,
            customer_currency="RUB",
            purchase_currency="AMD",
            identification_number="AB1234567",
            citizenship="Armenia",
        )
        return order.resume_token
    finally:
        conn.close()


def test_resuming_a_period_code_none_am_order_reaches_summary_not_the_legacy_period_screen():
    resume_token = _create_am_order_directly(price_customer_minor=500000)
    client = TestClient(app)
    response = client.get(f"/o/{resume_token}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/o/{resume_token}/summary"


def test_summary_shows_duration_in_days_for_a_period_code_none_am_order():
    resume_token = _create_am_order_directly(price_customer_minor=500000)
    client = TestClient(app)
    response = client.get(f"/o/{resume_token}/summary")
    assert response.status_code == 200
    assert "10 дней" in response.text
    assert "None" not in response.text  # period_label must never render as the literal string "None"


# ---------------------------------------------------------------------------
# TR passenger_car 30d: first confirmed business price (2299 RUB, confirmed
# 2026-08-29). 45d/90d/180d/365d remain deliberately unpriced -- see
# config.yaml's pricing.TR.passenger_car.
# ---------------------------------------------------------------------------


def test_tr_30d_is_now_priced_and_selectable(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    periods = {p["code"]: p for p in response.json()}
    assert periods["30d"]["is_priced"] is True
    assert periods["30d"]["price_rub"] == 2299


def test_tr_45_90_now_priced_180_365_remain_unselectable(real_config):
    client = TestClient(app)
    _start(client, "TR")
    for code in ("180d", "365d"):
        response = client.post("/category-period", data={"category_code": "passenger_car", "period_code": code})
        assert response.status_code == 422, code
        assert "Цена для этого периода пока не настроена" in response.text, code

    for code, expected_minor in (("45d", 299900), ("90d", 399900)):
        response = client.post(
            "/category-period", data={"category_code": "passenger_car", "period_code": code}, follow_redirects=False
        )
        assert response.status_code == 303, code
        draft = _read_draft(client)
        assert draft["period_code"] == code
        assert draft["price_customer_minor"] == expected_minor, code


def test_tr_30d_category_period_submission_now_succeeds(real_config):
    client = TestClient(app)
    _start(client, "TR")
    response = client.post(
        "/category-period", data={"category_code": "passenger_car", "period_code": "30d"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"
    draft = _read_draft(client)
    assert draft["period_code"] == "30d"
    assert draft["price_customer_minor"] == 229900  # 2299 RUB in kopecks


def test_tr_30d_price_flows_through_draft_order_summary_and_payment(real_config):
    """Full round-trip: draft -> create_order -> Order -> summary -> payment,
    using TR's now-real 30d price. TR requires engine_power/model_year
    (vehicle) and date_of_birth (policyholder) as of Step 4 -- supplied
    here so the flow actually reaches order creation."""
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "TR")
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"})
    client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "TR299AA",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
            "engine_power": "150",
            "model_year": "2020",
        },
    )
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_30d_price", date_of_birth="1990-05-20"),
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
    assert order.period_code == "30d"
    assert order.price_customer_minor == 229900

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert "2 299" in summary.text
    assert "30 дней" in summary.text

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert "2 299" in payment.text


@pytest.mark.parametrize(
    "period_code,expected_minor,expected_price_text,duration_days",
    [
        ("45d", 299900, "2 999", 45),
        ("90d", 399900, "3 999", 90),
    ],
)
def test_tr_45d_and_90d_full_flow(real_config, period_code, expected_minor, expected_price_text, duration_days):
    """Same round-trip as 30d above, now that 45d (2999 RUB) and 90d (3999
    RUB) are also confirmed prices -- including the exact end_date TR's own
    FixedDurationDateRule computes for each (start + N days, no month-based
    math involved for either)."""
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    _start(client, "TR")
    response = client.post(
        "/category-period",
        data={"category_code": "passenger_car", "period_code": period_code},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/date"
    draft = _read_draft(client)
    assert draft["price_customer_minor"] == expected_minor

    client.post("/date", data={"start_date": _iso(0)}, follow_redirects=False)
    draft = _read_draft(client)
    assert draft["end_date"] == _iso(duration_days)

    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": f"TR{period_code.upper()}AA",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
            "engine_power": "150",
            "model_year": "2020",
        },
    )
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram=f"@tr_{period_code}_price", date_of_birth="1990-05-20"),
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
    assert order.period_code == period_code
    assert order.price_customer_minor == expected_minor
    assert order.end_date.isoformat() == _iso(duration_days)

    summary = client.get(f"/o/{resume_token}/summary")
    assert summary.status_code == 200
    assert expected_price_text in summary.text

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert expected_price_text in payment.text


def test_tr_full_flow_starting_from_the_actual_homepage_link(real_config):
    """Public TR launch: the homepage card's own href (not a hand-typed
    /start?country=TR) is what's followed here, all the way to a paid-
    price-visible payment screen -- proves the whole chain end-to-end
    rather than just the country-aware routing plumbing in isolation."""
    from app.orders.repository import get_order_by_token
    from policyholder_helpers import valid_policyholder_data

    client = TestClient(app)
    home = client.get("/")
    assert 'href="/start?country=TR"' in home.text

    client.get("/start?country=TR", follow_redirects=False)
    client.post("/category-period", data={"category_code": "passenger_car", "period_code": "30d"}, follow_redirects=False)
    client.post("/date", data={"start_date": _iso(0)})
    client.post("/method", data={"choice": "manual"})
    client.post(
        "/vehicle",
        data={
            "registration_number": "TR911AA",
            "identifier_type": "vin",
            "identifier": "JT123456789012345",
            "manufacturer_id": str(_manufacturer_id),
            "model_id": str(_model_id),
            "engine_power": "150",
            "model_year": "2020",
        },
    )
    response = client.post(
        "/policyholder",
        data=valid_policyholder_data(contact_telegram="@tr_from_homepage", date_of_birth="1990-05-20"),
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
    assert order.price_customer_minor == 229900

    client.post(f"/o/{resume_token}/summary", data={"action": "pay"})
    payment = client.get(f"/o/{resume_token}/payment")
    assert payment.status_code == 200
    assert "2 299" in payment.text


def test_ge_pricing_unaffected_by_tr_30d_price(real_config):
    """Regression guard: adding TR's first real price must not touch GE's
    own real production prices (confirmed 2026-08-13)."""
    client = TestClient(app)
    response = client.get("/api/periods", params={"category_code": "passenger_car"})
    periods = {p["code"]: p["price_rub"] for p in response.json()}
    assert periods == {"15d": 1349, "30d": 2149, "90d": 3649}


def test_homepage_tr_teaser_stays_the_cheapest_priced_period(real_config):
    """The homepage teaser is computed from whatever's actually priced
    (see app.web.routes.landing -- min() over is_priced periods, never
    hardcoded), so it must still read "от 2 299 ₽" now that 45d/90d are
    also priced -- 2299 remains the cheapest of the three."""
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "от 2 299 ₽" in response.text
