"""Customer reply composition and invariant verification.

Guarantees that monetary figures in customer replies originate strictly from verified state.
"""
import re
from typing import Dict, Any, Set

BLOCK_EXPLANATIONS = {
    "NO_RETURN": "we don't have a return on record for this order",
    "RETURN_REJECTED": "the return was rejected",
    "OUTSIDE_WINDOW": "the return was started outside the return window that applied to this order",
    "RETURN_NOT_RECEIVED": "we haven't received your return yet, so a refund can't start until it arrives",
    "INSPECTION_PENDING": "your return has arrived but its inspection isn't finished yet",
    "INSPECTION_FAILED": "the returned item did not pass inspection",
}


def inr(minor: int) -> str:
    """Format minor units (paise) into INR string."""
    return f"₹{minor // 100:,}.{minor % 100:02d}"


def usd(minor: int) -> str:
    """Format minor units (cents) into USD string."""
    return f"${minor // 100:,}.{minor % 100:02d}"


def format_money(minor: int, currency: str = "INR") -> str:
    """Format minor currency units according to currency code."""
    if currency.upper() == "USD":
        return usd(minor)
    return inr(minor)


def compose_reply(outcome: str, context: Dict[str, Any]) -> str:
    """Generate deterministically verified reply according to outcome and context."""
    oid = context.get("order_id") or "your order"
    curr = context.get("currency", "INR")

    if outcome == "needs_order_id":
        return "Could you tell us the order number (for example ord_1234) this is about?"

    if outcome == "order_not_found":
        return "We couldn't find that order on your account. Please check the order number."

    if outcome == "order_status":
        sh = context.get("shipment", {})
        status = sh.get("status", "in transit")
        update_time = sh.get("last_update_at", "recently")
        detail = sh.get("detail", "Processing in fulfillment system.")
        return f"Order {oid} is currently '{status}'. Latest update ({update_time}): {detail}."

    if outcome == "order_status_unavailable":
        return f"We couldn't retrieve the latest shipment status for order {oid} right now. Please try again shortly."

    if outcome == "already_processing":
        op = context.get("detail", {}).get("op", {})
        amt = format_money(op.get("amount_minor", 0), curr)
        status = op.get("status", "processing")
        created = op.get("created_at", "recently")
        return (
            f"A refund of {amt} for order {oid} is already in progress "
            f"(status: {status}, started {created}). We have not issued a second refund."
        )

    if outcome == "already_refunded":
        op = context.get("detail", {}).get("op", {})
        amt = format_money(op.get("amount_minor", 0), curr)
        created = op.get("created_at", "recently")
        ref = op.get("provider_reference", "N/A")
        return (
            f"A refund of {amt} for order {oid} was recorded as succeeded "
            f"(started {created}, ref {ref}). Banks can take a few days to show it. "
            f"We have not issued another refund."
        )

    if outcome == "blocked":
        code = context.get("reason_code", "")
        why = BLOCK_EXPLANATIONS.get(code, "it doesn't meet our refund conditions")
        refs = context.get("policy_refs", [])
        ref_text = f" (Policy reference: {', '.join(refs)}.)" if refs else ""
        return f"We can't issue a refund for order {oid} right now because {why}.{ref_text} We can pass this to a specialist if you'd like."

    if outcome == "refund_succeeded":
        ex = context.get("execution", {})
        amt = format_money(ex.get("amount_minor", 0), curr)
        ref = ex.get("provider_reference", "")
        return f"The payment provider confirmed your refund of {amt} (ref {ref})."

    if outcome == "refund_submitted":
        ex = context.get("execution", {})
        amt = format_money(ex.get("amount_minor", 0), curr)
        ref = ex.get("provider_reference", "")
        return (
            f"The payment provider accepted your refund request of {amt} (ref {ref}). "
            f"It is still processing; we don't have final confirmation yet."
        )

    if outcome == "refund_unknown":
        return (
            "We couldn't confirm whether your refund went through, so we're checking with the payment provider. "
            "We will not issue a duplicate."
        )

    if outcome == "refund_failed":
        return "The payment provider declined the refund request. We've passed your case to a specialist."

    if outcome == "rejected":
        return "A reviewer did not approve the proposed refund. We've passed your case to a specialist."

    if outcome == "approval_expired":
        return "The approval for your refund expired before it could be processed. A specialist will follow up."

    if outcome == "revalidation_failed":
        return "Your case changed while it was under review, so we paused the refund and passed it to a specialist."

    if outcome == "replacement_approved":
        sku = context.get("replacement_sku", "replacement item")
        return f"A complimentary replacement ({sku}) for order {oid} has been approved and queued for dispatch."

    return "We've passed your case to a human specialist who will follow up."


def verify_reply(reply: str, allowed_amounts: Set[int]) -> str:
    """Financial invariant check: verify that all currency amounts are strictly authorized in state."""
    # Check INR ₹
    for m in re.findall(r"₹([\d,]+)\.(\d\d)", reply):
        val = int(m[0].replace(",", "")) * 100 + int(m[1])
        if val not in allowed_amounts:
            return "We've passed your case to a human specialist who will follow up."

    # Check USD $
    for m in re.findall(r"\$([\d,]+)\.(\d\d)", reply):
        val = int(m[0].replace(",", "")) * 100 + int(m[1])
        if val not in allowed_amounts:
            return "We've passed your case to a human specialist who will follow up."

    return reply
