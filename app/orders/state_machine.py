"""Order status enum + allowed transitions.

Only draft / data_completed / awaiting_payment are reachable in this phase.
The remaining statuses are defined now so the schema/enum does not need a
breaking change when payment review, fulfillment and delivery are added in
later phases.
"""

from enum import Enum


class OrderStatus(str, Enum):
    DRAFT = "draft"
    DATA_COMPLETED = "data_completed"
    AWAITING_PAYMENT = "awaiting_payment"
    PAYMENT_REVIEW = "payment_review"
    PAID = "paid"
    PROCESSING = "processing"
    POLICY_READY = "policy_ready"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.DRAFT: {OrderStatus.DATA_COMPLETED, OrderStatus.CANCELLED},
    OrderStatus.DATA_COMPLETED: {OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELLED},
    OrderStatus.AWAITING_PAYMENT: {OrderStatus.PAYMENT_REVIEW, OrderStatus.CANCELLED},
    OrderStatus.PAYMENT_REVIEW: {OrderStatus.PAID, OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
    OrderStatus.PROCESSING: {OrderStatus.POLICY_READY, OrderStatus.CANCELLED},
    OrderStatus.POLICY_READY: {OrderStatus.COMPLETED},
    OrderStatus.COMPLETED: set(),
    OrderStatus.CANCELLED: set(),
}


class InvalidTransition(ValueError):
    pass


def ensure_transition_allowed(current: OrderStatus, target: OrderStatus) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"Cannot transition from '{current.value}' to '{target.value}'")
