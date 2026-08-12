"""Builds the progress-nav step list (Транспорт и срок -> ... -> Проверка)
shared by every checkout screen.

Three states, deliberately kept independent of each other:

- "active"  -- exactly one step, the one being viewed right now. Always
  the solid blue circle; never has a url (no point linking to yourself).
- "done"    -- the step's data has already been provided. Neutral/gray
  circle -- NOT blue -- whether or not it happens to have a url. Blue is
  reserved for "active" alone; a done step that also has a url just adds
  a pointer cursor and a hover highlight, never a persistent blue outline.
- "future"  -- the step's data hasn't been provided yet. Dim gray,
  never clickable.

Completedness and clickability are computed separately on purpose: a step
can be done-but-not-clickable (e.g. "Способ ввода" post-order -- see
build_order_steps) and a step can be clickable while visually "ahead" of
the current one (e.g. /vehicle from /method, since both share the same
has_dates guard). Neither is a reason to paint it blue.
"""

from typing import TypedDict

STEP_LABELS = [
    "Транспорт и срок",
    "Дата",
    "Способ ввода",
    "Данные авто",
    "Страховщик",
    "Проверка",
]


class Step(TypedDict):
    index: int
    label: str
    state: str  # "done" | "active" | "future"
    url: str | None


def _assemble(current_step: int, urls: dict[int, str | None], completed: set[int]) -> list[Step]:
    steps: list[Step] = []
    for index, label in enumerate(STEP_LABELS, start=1):
        if index == current_step:
            state, url = "active", None
        elif index in completed:
            state, url = "done", urls.get(index)
        else:
            state, url = "future", None
        steps.append({"index": index, "label": label, "state": state, "url": url})
    return steps


def build_draft_steps(draft: dict | None, current_step: int) -> list[Step]:
    """Pre-order wizard (no order created yet). A step counts as done, and
    its url unlocked, once the draft holds what that route's own guard
    requires (_require_draft_keys in checkout_routes.py) -- so a step is
    never offered as reachable when its GET handler would just redirect
    the user straight back out of it."""
    draft = draft or {}
    has_category_period = draft.get("vehicle_category_code") is not None and draft.get("period_code") is not None
    has_dates = draft.get("start_date") is not None and draft.get("end_date") is not None
    has_vehicle = draft.get("manufacturer_id") is not None and draft.get("model_id") is not None

    urls = {
        1: "/category-period",
        2: "/date" if has_category_period else None,
        3: "/method" if has_dates else None,
        4: "/vehicle" if has_dates else None,
        5: "/policyholder" if has_vehicle else None,
        6: None,  # no order yet -- nothing to review
    }
    completed = {index for index, url in urls.items() if url}
    return _assemble(current_step, urls, completed)


def build_order_steps(order, current_step: int) -> list[Step]:
    """Post-order: every field has a value (the order exists), so every
    step counts as done. "Способ ввода" (3) has no edit route by design --
    it's how data entry started, not a policy field -- so it's done but
    not clickable; every other step has a real order-scoped edit route."""
    urls = {
        1: f"/o/{order.resume_token}/edit-coverage",
        2: f"/o/{order.resume_token}/edit-date",
        3: None,
        4: f"/o/{order.resume_token}/edit-vehicle",
        5: f"/o/{order.resume_token}/edit-policyholder",
        6: f"/o/{order.resume_token}/summary",
    }
    completed = {1, 2, 3, 4, 5, 6}
    return _assemble(current_step, urls, completed)
