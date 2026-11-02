"""Two-Phase Reserve-Then-Call Financial Settlement Engine.

Lifecycle:
1. Lock order & proposal, revalidate conditions, insert 'reserved' RefundOperation row.
2. Commit DB transaction (never hold locks or DB transactions across network I/O).
3. Call external Payment Gateway with a unique, deterministically generated idempotency key.
4. Update local state to 'succeeded', 'submitted', or 'unknown' (for automatic key reconciliation).
"""
import hashlib
import json
from typing import Dict, Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import (
    ActionProposal,
    Approval,
    Order,
    RefundOperation,
    Ticket,
    WorkflowRun,
    audit,
    utcnow,
)
from .payments import PaymentClient, ProviderRejected, ProviderUncertain
from .replies import format_money
from .service import op_dict, validate_refund


class RefundBlocked(Exception):
    """Raised when an approval gate, revalidation rule, or payload integrity check fails."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def payload_hash(payload: Dict[str, Any]) -> str:
    """Generate SHA-256 digest of canonical JSON proposal payload."""
    canonical_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def _get_run_id(s: Session, op: RefundOperation) -> Optional[str]:
    if not op.proposal_id:
        return None
    prop = s.get(ActionProposal, op.proposal_id)
    return prop.run_id if prop else None


def idem_key(proposal_id: str) -> str:
    """Stable idempotency key bound to the proposal identifier."""
    return f"refund-{proposal_id}"


def execute_approved_refund(
    session_factory, proposal_id: str, pay: PaymentClient
) -> Dict[str, Any]:
    """Execute settlement after verifying human reviewer approval and state invariants."""
    try:
        with session_factory.begin() as s:
            prop = s.get(ActionProposal, proposal_id)
            appr = s.scalars(
                select(Approval).where(Approval.proposal_id == proposal_id)
            ).first()

            if prop is None or appr is None:
                raise RefundBlocked("NO_PROPOSAL")
            if appr.decision != "approved":
                raise RefundBlocked("NOT_APPROVED")
            if appr.expires_at < (appr.decided_at or utcnow()):
                raise RefundBlocked("APPROVAL_EXPIRED")
            if payload_hash(prop.payload) != prop.payload_hash:
                raise RefundBlocked("PAYLOAD_CHANGED_AFTER_APPROVAL")

            p = prop.payload
            order = s.execute(
                select(Order).where(Order.id == p["order_id"]).with_for_update()
            ).scalar_one()

            # Prevent duplicate execution on retries
            existing = s.scalars(
                select(RefundOperation).where(
                    RefundOperation.idempotency_key == idem_key(proposal_id)
                )
            ).first()
            if existing:
                return op_dict(existing) | {"created": False}

            ok, code = validate_refund(s, order, p["amount_minor"], p["reason_code"])
            if not ok:
                audit(
                    s,
                    prop.run_id,
                    "system",
                    "refund_blocked_at_validation",
                    {"code": code},
                )
                raise RefundBlocked(code)

            op = RefundOperation(
                id=f"op_{proposal_id[-12:]}",
                order_id=order.id,
                proposal_id=proposal_id,
                amount_minor=p["amount_minor"],
                currency=order.currency,
                idempotency_key=idem_key(proposal_id),
                status="reserved",
            )
            s.add(op)
            audit(
                s,
                prop.run_id,
                "system",
                "refund_reserved",
                {"op": op.id, "amount_minor": op.amount_minor, "currency": op.currency},
            )
            op_id = op.id
    except IntegrityError:
        # Gracefully handle race condition if another thread committed the idempotency key
        with session_factory() as s:
            op = s.scalars(
                select(RefundOperation).where(
                    RefundOperation.idempotency_key == idem_key(proposal_id)
                )
            ).one()
            return op_dict(op) | {"created": False}

    # Step 2: Call payment processor outside database transaction
    return _call_provider(session_factory, op_id, pay) | {"created": True}


def _call_provider(session_factory, op_id: str, pay: PaymentClient) -> Dict[str, Any]:
    with session_factory() as s:
        op = s.get(RefundOperation, op_id)
        key, order_id, amount = op.idempotency_key, op.order_id, op.amount_minor

    status, ref, note = "unknown", None, None
    try:
        resp = pay.create_refund(key, order_id, amount)
        status = "succeeded" if resp.get("status") == "succeeded" else "submitted"
        ref = resp.get("refund_id")
    except ProviderUncertain as exc:
        note = str(exc)
    except ProviderRejected as exc:
        status, note = "failed", str(exc)

    with session_factory.begin() as s:
        op = s.get(RefundOperation, op_id)
        op.status = status
        op.provider_reference = ref or op.provider_reference
        audit(
            s,
            _get_run_id(s, op),
            "system",
            "refund_provider_result",
            {"op": op_id, "status": status, "note": note},
        )
        return op_dict(op)


def reconcile(session_factory, op_id: str, pay: PaymentClient) -> Dict[str, Any]:
    """Reconcile an in-flight or uncertain transaction against the provider by idempotency key."""
    with session_factory() as s:
        op = s.get(RefundOperation, op_id)
        if not op or op.status not in ("reserved", "unknown", "submitted"):
            return op_dict(op) if op else {}
        prior_status, key = op.status, op.idempotency_key

    try:
        rec = pay.get_by_key(key)
    except ProviderUncertain:
        if prior_status == "submitted":
            with session_factory() as s:
                return op_dict(s.get(RefundOperation, op_id))
        return _set(session_factory, op_id, "unknown", None)

    if rec is None:
        # Gateway has no record of the transaction: retry with the same key safely
        return _call_provider(session_factory, op_id, pay)

    new_status = "succeeded" if rec.get("status") == "succeeded" else "submitted"
    return _set(session_factory, op_id, new_status, rec.get("refund_id"))


def _set(session_factory, op_id: str, status: str, ref: Optional[str]) -> Dict[str, Any]:
    with session_factory.begin() as s:
        op = s.get(RefundOperation, op_id)
        changed = op.status != status
        op.status = status
        op.provider_reference = ref or op.provider_reference
        run_id = _get_run_id(s, op)
        audit(s, run_id, "system", "refund_reconciled", {"op": op_id, "status": status})

        if status == "succeeded" and changed and run_id:
            # Update customer ticket now that settlement is fully confirmed
            ticket = (
                s.query(Ticket)
                .join(WorkflowRun, WorkflowRun.ticket_id == Ticket.id)
                .filter(WorkflowRun.id == run_id)
                .first()
            )
            if ticket:
                ticket.status = "resolved"
                amt_str = format_money(op.amount_minor, op.currency)
                ticket.public_note = (
                    f"The payment provider confirmed your refund of {amt_str} "
                    f"(ref {op.provider_reference})."
                )
                audit(s, run_id, "system", "ticket_updated", {"status": "resolved"})

        return op_dict(op)
