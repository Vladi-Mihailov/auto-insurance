import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class IssuanceStatus(str, Enum):
    """Sub-status of one Order's TPL issuance -- deliberately NOT a new
    top-level app.orders.state_machine.OrderStatus value (see the delivery
    report's ARCHITECTURE section for why): the order's own status only
    ever needs to know PAID vs PROCESSING, while this tracks the finer-
    grained progress of getting there."""

    PENDING = "pending"  # tpl_uid allocated; POST /api/policies not yet successful
    APPLICATION_CREATED = "application_created"  # POST /api/policies succeeded
    BOG_LINK_READY = "bog_link_ready"  # GET /ecommerce/bog succeeded, URL stored
    OPERATOR_REPORTED_PAID = "operator_reported_paid"  # manual "Оплата TPL завершена"
    FAILED = "failed"


@dataclass
class TplIssuance:
    id: int
    order_id: int
    tpl_uid: str
    tpl_product_id: int | None
    tpl_purchase_price_gel: Decimal | None
    bog_payment_url: str | None
    issuance_status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    # TPL-server-issued identifier minted by the BOG handoff (GET /ecommerce/bog)
    # -- confirmed DISTINCT from tpl_uid via real HAR evidence. Required to call
    # GET /api/policies/{o.id}. Overwritten on every successful BOG-link
    # refresh (a repeat "Получить новую ссылку" mints a fresh one) -- see
    # service._refresh_bog_link, the only writer.
    tpl_o_id: str | None = None
    # Populated only once GET /api/policies/{o.id} confirms the policy is
    # actually issued (see service.retrieve_issued_policy) -- all six of
    # these plus policy_retrieved_at are set together, atomically, by
    # repository.mark_policy_retrieved.
    policy_number: str | None = None
    tpl_policy_id: int | None = None
    policy_document_url: str | None = None
    invoice_document_url: str | None = None
    additional_terms_document_url: str | None = None
    policy_retrieved_at: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TplIssuance":
        return cls(
            id=row["id"],
            order_id=row["order_id"],
            tpl_uid=row["tpl_uid"],
            tpl_product_id=row["tpl_product_id"],
            tpl_purchase_price_gel=Decimal(row["tpl_purchase_price_gel"]) if row["tpl_purchase_price_gel"] else None,
            bog_payment_url=row["bog_payment_url"],
            issuance_status=row["issuance_status"],
            last_error=row["last_error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            tpl_o_id=row["tpl_o_id"],
            policy_number=row["policy_number"],
            tpl_policy_id=row["tpl_policy_id"],
            policy_document_url=row["policy_document_url"],
            invoice_document_url=row["invoice_document_url"],
            additional_terms_document_url=row["additional_terms_document_url"],
            policy_retrieved_at=datetime.fromisoformat(row["policy_retrieved_at"]) if row["policy_retrieved_at"] else None,
        )

    @property
    def is_bog_link_ready(self) -> bool:
        return self.issuance_status == IssuanceStatus.BOG_LINK_READY.value

    @property
    def is_operator_reported_paid(self) -> bool:
        return self.issuance_status == IssuanceStatus.OPERATOR_REPORTED_PAID.value

    @property
    def is_failed(self) -> bool:
        return self.issuance_status == IssuanceStatus.FAILED.value

    @property
    def application_already_created(self) -> bool:
        """True once POST /api/policies has succeeded at least once for
        this order -- the guard that keeps a repeat admin click from ever
        sending a second one (see service.issue_tpl_policy)."""
        return self.issuance_status in (
            IssuanceStatus.APPLICATION_CREATED.value,
            IssuanceStatus.BOG_LINK_READY.value,
            IssuanceStatus.OPERATOR_REPORTED_PAID.value,
        )

    @property
    def is_policy_retrieved(self) -> bool:
        """True once GET /api/policies/{o.id} has successfully confirmed
        issuance and the policy/document fields are persisted -- the guard
        service.retrieve_issued_policy checks before calling TPL again (see
        its own docstring for why a repeat call must not re-fetch by
        default)."""
        return self.policy_retrieved_at is not None


@dataclass(frozen=True)
class LiveProduct:
    """One product from TPL's own live GET /api/core/categories?embed=products
    -- resolved fresh for every issuance attempt, never cached/hardcoded (see
    service.resolve_product)."""

    product_id: int
    period: int
    period_type: str  # "D" or "Y", as TPL returns it
    price_gel: Decimal
    min_date: date
    max_date: date
