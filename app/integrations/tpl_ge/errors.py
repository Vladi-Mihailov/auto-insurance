"""Operator-facing exceptions for GE -> TPL policy issuance.

Every one of these is meant to be caught at the admin route and shown to the
operator as a plain-language message (see app.web.admin_routes) -- never
retried automatically, never papered over with a fabricated value.
"""


class TplIssuanceError(Exception):
    """Base class for every issuance failure. str(exc) is always safe to
    show directly to an operator (no secrets, no card/OTP/token data)."""


class ProductNotFoundError(TplIssuanceError):
    """No live TPL product matches the order's category/period, or the
    order's start_date falls outside the live product's minDate/maxDate
    window. Processing this order must stop here -- never fall back to a
    stale/guessed productId or price."""


class MissingRequiredDataError(TplIssuanceError):
    """A field TPL's API requires has no value on our side (empty contact
    phone, unresolvable citizenship, unsupported legal-entity owner, etc).
    Never silently substitute a placeholder -- surface exactly which field."""


class VisitorIdNotConfiguredError(TplIssuanceError):
    """settings.tpl_ge.static_visitor_id is unset. Never fabricate/spoof a
    fingerprint value -- this is a deployment/configuration gap, not a
    per-order data gap."""


class TplApplicationError(TplIssuanceError):
    """POST /api/policies did not return the expected success response."""


class BogHandoffError(TplIssuanceError):
    """GET /ecommerce/bog did not return the expected 302 + Location."""


class PolicyNotReadyError(TplIssuanceError):
    """GET /api/policies/{o.id} responded, but the policy isn't issued yet
    (no policyNumber / no documents) -- confirmed real behaviour is
    unobserved for this exact window (discovery only ever captured the
    ALREADY-issued state), so this is a deliberately distinct, non-fatal
    outcome from PolicyRetrievalError below. Callers must treat this as
    "try again later", never as a permanent failure -- see
    service.retrieve_issued_policy."""


class PolicyRetrievalError(TplIssuanceError):
    """GET /api/policies/{o.id} (or .../documents) returned something
    genuinely unexpected -- a non-2xx status or an unparseable body. Unlike
    PolicyNotReadyError, this is a real anomaly worth surfacing."""
