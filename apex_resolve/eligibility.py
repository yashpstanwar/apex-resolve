"""Deterministic refund and claim eligibility assessment engine.

Zero external network I/O, zero LLM variance: purely functional and exhaustive.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


class Verdict(str, Enum):
    ELIGIBLE_NEW_REFUND = "eligible_new_refund"
    ALREADY_PROCESSING = "already_processing"
    ALREADY_REFUNDED = "already_refunded"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    reason_code: str
    amount_minor: int = 0
    currency: str = "USD"
    detail: Dict[str, Any] = field(default_factory=dict)


def assess_refund(
    *,
    paid_amount_minor: int,
    currency: str = "USD",
    delivered_at: Optional[datetime],
    ret: Optional[Dict[str, Any]],
    refund_ops: List[Dict[str, Any]],
    window_days: int = 30,
) -> Assessment:
    """Evaluate whether an order is eligible for a refund according to verified records."""
    # 1. Guard against duplicate payout if already completed
    for op in refund_ops:
        if op.get("status") == "succeeded":
            return Assessment(
                verdict=Verdict.ALREADY_REFUNDED,
                reason_code="REFUND_SUCCEEDED",
                currency=currency,
                detail={"op": op},
            )

    # 2. Guard against concurrent / pending refund operations
    for op in refund_ops:
        if op.get("status") in ("reserved", "submitted", "unknown"):
            return Assessment(
                verdict=Verdict.ALREADY_PROCESSING,
                reason_code="REFUND_IN_PROGRESS",
                currency=currency,
                detail={"op": op},
            )

    # 3. Check physical return requirement
    if ret is None:
        return Assessment(
            verdict=Verdict.BLOCKED,
            reason_code="NO_RETURN",
            currency=currency,
        )

    if ret.get("status") == "rejected":
        return Assessment(
            verdict=Verdict.BLOCKED,
            reason_code="RETURN_REJECTED",
            currency=currency,
        )

    # 4. Verify delivery timestamp existence
    if delivered_at is None:
        return Assessment(
            verdict=Verdict.NEEDS_REVIEW,
            reason_code="MISSING_DELIVERY_DATE",
            currency=currency,
        )

    # 5. Check return window validity
    initiated_at = ret.get("initiated_at")
    if initiated_at and (initiated_at - delivered_at > timedelta(days=window_days)):
        return Assessment(
            verdict=Verdict.BLOCKED,
            reason_code="OUTSIDE_WINDOW",
            currency=currency,
            detail={"window_days": window_days},
        )

    # 6. Physical arrival verification
    return_status = ret.get("status")
    if return_status in ("initiated", "in_transit"):
        return Assessment(
            verdict=Verdict.BLOCKED,
            reason_code="RETURN_NOT_RECEIVED",
            currency=currency,
            detail={"return_status": return_status},
        )

    # 7. Inspection gate
    if return_status == "received":
        if ret.get("received_at") is None:
            return Assessment(
                verdict=Verdict.NEEDS_REVIEW,
                reason_code="RECEIVED_WITHOUT_TIMESTAMP",
                currency=currency,
            )

        insp = ret.get("inspection_status")
        if insp == "pending":
            return Assessment(
                verdict=Verdict.BLOCKED,
                reason_code="INSPECTION_PENDING",
                currency=currency,
            )
        if insp == "failed":
            return Assessment(
                verdict=Verdict.BLOCKED,
                reason_code="INSPECTION_FAILED",
                currency=currency,
            )
        if insp == "passed":
            return Assessment(
                verdict=Verdict.ELIGIBLE_NEW_REFUND,
                reason_code="RETURN_RECEIVED_INSPECTION_PASSED",
                amount_minor=paid_amount_minor,
                currency=currency,
            )

    return Assessment(
        verdict=Verdict.NEEDS_REVIEW,
        reason_code="UNRECOGNISED_RETURN_STATE",
        currency=currency,
    )
