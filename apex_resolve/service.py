"""Domain services for order ownership verification, return assessment, and refund validation."""
from typing import Optional, Tuple, Dict, Any, List
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Order, RefundOperation, ReturnRecord
from .eligibility import Verdict, Assessment, assess_refund
from .policies import find_policy

ACTIVE_REFUND_STATUSES = ("reserved", "submitted", "unknown", "succeeded")


def op_dict(op: RefundOperation) -> Dict[str, Any]:
    """Serialize a RefundOperation into a clean dictionary representation."""
    return {
        "id": op.id,
        "status": op.status,
        "amount_minor": op.amount_minor,
        "currency": op.currency,
        "provider_reference": op.provider_reference,
        "created_at": op.created_at.date().isoformat() if op.created_at else None,
    }


def get_owned_order(s: Session, customer_id: str, order_id: str) -> Optional[Order]:
    """Strict authorization gate: foreign and non-existent orders return None identically."""
    return s.scalars(
        select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
    ).first()


def assess_order_refund(s: Session, order: Order) -> Tuple[Assessment, Any]:
    """Retrieve database state and evaluate deterministic refund eligibility for an order."""
    ret = s.scalars(
        select(ReturnRecord)
        .where(ReturnRecord.order_id == order.id)
        .order_by(ReturnRecord.initiated_at.desc())
    ).first()

    ret_dict = (
        None
        if ret is None
        else {
            "status": ret.status,
            "initiated_at": ret.initiated_at,
            "received_at": ret.received_at,
            "inspection_status": ret.inspection_status,
        }
    )

    ops = [
        op_dict(o)
        for o in s.scalars(
            select(RefundOperation).where(RefundOperation.order_id == order.id)
        )
    ]

    doc = find_policy(s, "refund", order.ordered_at)
    if doc is None:
        return Assessment(
            verdict=Verdict.NEEDS_REVIEW,
            reason_code="NO_APPLICABLE_POLICY",
            currency=order.currency,
        ), None

    window_days = doc.meta.get("window_days", 30)
    assessment = assess_refund(
        paid_amount_minor=order.paid_amount_minor,
        currency=order.currency,
        delivered_at=order.delivered_at,
        ret=ret_dict,
        refund_ops=ops,
        window_days=window_days,
    )
    return assessment, doc


def validate_refund(
    s: Session, order: Order, amount_minor: int, reason_code: str
) -> Tuple[bool, str]:
    """Re-validate immediately before locking funds or calling payment gateway."""
    ops = s.scalars(
        select(RefundOperation).where(RefundOperation.order_id == order.id)
    ).all()

    if any(o.status in ACTIVE_REFUND_STATUSES for o in ops):
        return False, "REFUND_ALREADY_ACTIVE"

    if amount_minor <= 0 or amount_minor > order.paid_amount_minor:
        return False, "AMOUNT_EXCEEDS_PAID"

    if reason_code == "DAMAGED_ITEM":
        return True, ""

    assessment, _ = assess_order_refund(s, order)
    if assessment.verdict != Verdict.ELIGIBLE_NEW_REFUND:
        return False, assessment.reason_code

    if assessment.amount_minor != amount_minor:
        return False, "AMOUNT_MISMATCH"

    return True, ""
