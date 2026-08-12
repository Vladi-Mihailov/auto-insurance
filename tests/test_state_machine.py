import pytest

from app.orders.state_machine import InvalidTransition, OrderStatus, ensure_transition_allowed


def test_allowed_transitions_do_not_raise():
    ensure_transition_allowed(OrderStatus.DRAFT, OrderStatus.DATA_COMPLETED)
    ensure_transition_allowed(OrderStatus.DATA_COMPLETED, OrderStatus.AWAITING_PAYMENT)
    ensure_transition_allowed(OrderStatus.PAYMENT_REVIEW, OrderStatus.AWAITING_PAYMENT)


def test_disallowed_transition_raises():
    with pytest.raises(InvalidTransition):
        ensure_transition_allowed(OrderStatus.DRAFT, OrderStatus.PAID)


def test_terminal_statuses_have_no_outgoing_transitions():
    with pytest.raises(InvalidTransition):
        ensure_transition_allowed(OrderStatus.COMPLETED, OrderStatus.DRAFT)
    with pytest.raises(InvalidTransition):
        ensure_transition_allowed(OrderStatus.CANCELLED, OrderStatus.DRAFT)


def test_paid_cannot_skip_straight_to_policy_ready():
    with pytest.raises(InvalidTransition):
        ensure_transition_allowed(OrderStatus.PAID, OrderStatus.POLICY_READY)
