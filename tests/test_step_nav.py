"""build_draft_steps/build_order_steps decide which progress-nav steps are
"done" (completed) vs "future" (not reached), and separately which have a
url (clickable). Exactly one step is ever "active" (current); "done" must
never require a url, and having a url must never imply "active" -- see the
module docstring in app/web/step_nav.py for why."""

from app.web.step_nav import STEP_LABELS, build_draft_steps, build_order_steps


class _FakeOrder:
    def __init__(self, resume_token="tok123"):
        self.resume_token = resume_token


def _assert_exactly_one_active(steps):
    active = [s for s in steps if s["state"] == "active"]
    assert len(active) == 1, f"expected exactly one active step, got {active}"
    return active[0]


def test_fresh_session_only_step_one_is_reachable():
    steps = build_draft_steps({}, current_step=1)
    assert [s["state"] for s in steps] == ["active"] + ["future"] * 5
    assert steps[0]["url"] is None  # current step never links to itself
    assert all(s["url"] is None for s in steps[1:])
    _assert_exactly_one_active(steps)


def test_category_period_chosen_unlocks_date_step():
    draft = {"vehicle_category_code": "passenger_car", "period_code": "30d"}
    steps = build_draft_steps(draft, current_step=1)
    by_index = {s["index"]: s for s in steps}
    assert by_index[2]["url"] == "/date"
    assert by_index[2]["state"] == "done"
    assert by_index[3]["url"] is None  # method/vehicle need dates too
    assert by_index[3]["state"] == "future"
    _assert_exactly_one_active(steps)


def test_dates_chosen_unlocks_method_and_vehicle_even_though_vehicle_is_ahead_of_current_step():
    """Standing on /method (step 3), /vehicle (step 4) shares the exact same
    guard (start_date+end_date) -- it must already be clickable ("done"),
    not "future", even though its index is ahead of the current step."""
    draft = {
        "vehicle_category_code": "passenger_car",
        "period_code": "30d",
        "start_date": "2026-08-15",
        "end_date": "2026-09-14",
    }
    steps = build_draft_steps(draft, current_step=3)
    by_index = {s["index"]: s for s in steps}
    assert by_index[4]["url"] == "/vehicle"
    assert by_index[4]["state"] == "done"
    assert by_index[5]["url"] is None  # no manufacturer/model yet
    assert by_index[5]["state"] == "future"
    _assert_exactly_one_active(steps)


def test_manufacturer_and_model_chosen_unlocks_policyholder_only():
    draft = {
        "vehicle_category_code": "passenger_car",
        "period_code": "30d",
        "start_date": "2026-08-15",
        "end_date": "2026-09-14",
        "manufacturer_id": 12,
        "model_id": 361,
    }
    steps = build_draft_steps(draft, current_step=4)
    by_index = {s["index"]: s for s in steps}
    assert by_index[5]["url"] == "/policyholder"
    assert by_index[6]["url"] is None  # review step needs an order, not just a draft
    assert by_index[6]["state"] == "future"


def test_current_step_is_active_and_never_a_link_even_when_its_own_data_is_present():
    draft = {"vehicle_category_code": "passenger_car", "period_code": "30d"}
    steps = build_draft_steps(draft, current_step=2)
    assert steps[1]["state"] == "active"
    assert steps[1]["url"] is None
    _assert_exactly_one_active(steps)


def test_done_state_never_depends_on_index_being_behind_current():
    """A step "ahead" of current with a satisfied guard is "done", not
    "future" -- state tracks availability, not page position."""
    draft = {
        "vehicle_category_code": "passenger_car",
        "period_code": "30d",
        "start_date": "2026-08-15",
        "end_date": "2026-09-14",
        "manufacturer_id": 12,
        "model_id": 361,
    }
    steps = build_draft_steps(draft, current_step=1)
    by_index = {s["index"]: s for s in steps}
    for index in (2, 3, 4, 5):
        assert by_index[index]["state"] == "done", f"step {index} should be done, got {by_index[index]['state']}"
        assert by_index[index]["url"] is not None


def test_order_steps_every_step_but_method_has_an_edit_route():
    """Post-order: category/period, date, vehicle and policyholder all got
    real edit routes (section 7). "Способ ввода" deliberately has none --
    it's how data entry started, not a policy field."""
    order = _FakeOrder()
    steps = build_order_steps(order, current_step=4)
    by_index = {s["index"]: s for s in steps}
    assert by_index[1]["url"] == "/o/tok123/edit-coverage"
    assert by_index[2]["url"] == "/o/tok123/edit-date"
    assert by_index[3]["url"] is None
    assert by_index[5]["url"] == "/o/tok123/edit-policyholder"
    assert by_index[6]["url"] == "/o/tok123/summary"
    _assert_exactly_one_active(steps)


def test_order_steps_method_is_done_not_future_even_with_no_url():
    """Section 12: "completed-but-not-clickable" is its own normal state --
    it must render as "done" (neutral, reached), never as dim "future"."""
    order = _FakeOrder()
    steps = build_order_steps(order, current_step=6)
    method_step = steps[2]  # index 3, "Способ ввода"
    assert method_step["state"] == "done"
    assert method_step["url"] is None


def test_order_steps_on_review_screen_five_steps_are_clickable_one_is_not():
    order = _FakeOrder()
    steps = build_order_steps(order, current_step=6)
    clickable = {s["index"] for s in steps if s["url"]}
    assert clickable == {1, 2, 4, 5}
    _assert_exactly_one_active(steps)


def test_order_steps_active_step_has_no_url_and_is_the_only_active_one():
    order = _FakeOrder()
    for current in range(1, 7):
        steps = build_order_steps(order, current_step=current)
        active = _assert_exactly_one_active(steps)
        assert active["index"] == current
        assert active["url"] is None


def test_all_six_labels_present_in_order():
    steps = build_draft_steps({}, current_step=1)
    assert [s["label"] for s in steps] == STEP_LABELS
    assert len(STEP_LABELS) == 6
