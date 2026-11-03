"""Unit tests for deterministic refund eligibility and reply verification."""
from datetime import datetime, timedelta
import pytest

from apex_resolve.eligibility import Verdict, assess_refund
from apex_resolve.replies import compose_reply, inr, usd, verify_reply


def test_already_refunded_takes_precedence():
    delivered = datetime(2026, 1, 10)
    ret = {"status": "received", "initiated_at": datetime(2026, 1, 15), "received_at": datetime(2026, 1, 18), "inspection_status": "passed"}
    ops = [{"status": "succeeded", "amount_minor": 5000}]
    a = assess_refund(paid_amount_minor=5000, delivered_at=delivered, ret=ret, refund_ops=ops, window_days=30)
    assert a.verdict == Verdict.ALREADY_REFUNDED
    assert a.reason_code == "REFUND_SUCCEEDED"


def test_already_processing_blocks_new_operation():
    delivered = datetime(2026, 1, 10)
    ret = {"status": "received", "initiated_at": datetime(2026, 1, 15), "received_at": datetime(2026, 1, 18), "inspection_status": "passed"}
    for status in ("reserved", "submitted", "unknown"):
        ops = [{"status": status, "amount_minor": 5000}]
        a = assess_refund(paid_amount_minor=5000, delivered_at=delivered, ret=ret, refund_ops=ops, window_days=30)
        assert a.verdict == Verdict.ALREADY_PROCESSING
        assert a.reason_code == "REFUND_IN_PROGRESS"


def test_no_return_record_blocks_refund():
    a = assess_refund(paid_amount_minor=5000, delivered_at=datetime(2026, 1, 10), ret=None, refund_ops=[], window_days=30)
    assert a.verdict == Verdict.BLOCKED
    assert a.reason_code == "NO_RETURN"


def test_rejected_return_blocks_refund():
    ret = {"status": "rejected", "initiated_at": datetime(2026, 1, 12)}
    a = assess_refund(paid_amount_minor=5000, delivered_at=datetime(2026, 1, 10), ret=ret, refund_ops=[], window_days=30)
    assert a.verdict == Verdict.BLOCKED
    assert a.reason_code == "RETURN_REJECTED"


def test_missing_delivery_date_triggers_human_review():
    ret = {"status": "received", "initiated_at": datetime(2026, 1, 12)}
    a = assess_refund(paid_amount_minor=5000, delivered_at=None, ret=ret, refund_ops=[], window_days=30)
    assert a.verdict == Verdict.NEEDS_REVIEW
    assert a.reason_code == "MISSING_DELIVERY_DATE"


def test_outside_window_blocks_refund():
    delivered = datetime(2026, 1, 1)
    ret = {"status": "received", "initiated_at": datetime(2026, 2, 5)}  # 35 days later
    a = assess_refund(paid_amount_minor=5000, delivered_at=delivered, ret=ret, refund_ops=[], window_days=30)
    assert a.verdict == Verdict.BLOCKED
    assert a.reason_code == "OUTSIDE_WINDOW"


def test_return_in_transit_blocks_refund():
    delivered = datetime(2026, 1, 10)
    for s in ("initiated", "in_transit"):
        ret = {"status": s, "initiated_at": datetime(2026, 1, 15)}
        a = assess_refund(paid_amount_minor=5000, delivered_at=delivered, ret=ret, refund_ops=[], window_days=30)
        assert a.verdict == Verdict.BLOCKED
        assert a.reason_code == "RETURN_NOT_RECEIVED"


def test_inspection_pending_and_failed_blocks():
    delivered = datetime(2026, 1, 10)
    ret_pending = {"status": "received", "initiated_at": datetime(2026, 1, 12), "received_at": datetime(2026, 1, 15), "inspection_status": "pending"}
    a_pen = assess_refund(paid_amount_minor=5000, delivered_at=delivered, ret=ret_pending, refund_ops=[], window_days=30)
    assert a_pen.verdict == Verdict.BLOCKED
    assert a_pen.reason_code == "INSPECTION_PENDING"

    ret_failed = {"status": "received", "initiated_at": datetime(2026, 1, 12), "received_at": datetime(2026, 1, 15), "inspection_status": "failed"}
    a_fail = assess_refund(paid_amount_minor=5000, delivered_at=delivered, ret=ret_failed, refund_ops=[], window_days=30)
    assert a_fail.verdict == Verdict.BLOCKED
    assert a_fail.reason_code == "INSPECTION_FAILED"


def test_inspection_passed_grants_eligibility():
    delivered = datetime(2026, 1, 10)
    ret = {"status": "received", "initiated_at": datetime(2026, 1, 12), "received_at": datetime(2026, 1, 15), "inspection_status": "passed"}
    a = assess_refund(paid_amount_minor=249900, delivered_at=delivered, ret=ret, refund_ops=[], window_days=30)
    assert a.verdict == Verdict.ELIGIBLE_NEW_REFUND
    assert a.reason_code == "RETURN_RECEIVED_INSPECTION_PASSED"
    assert a.amount_minor == 249900


def test_currency_formatting():
    assert inr(249900) == "₹2,499.00"
    assert inr(500) == "₹5.00"
    assert usd(249900) == "$2,499.00"


def test_verify_reply_prevents_hallucinated_amounts():
    # Valid amount allowed
    safe = "Your refund of ₹2,499.00 has been verified."
    assert verify_reply(safe, {249900}) == safe

    # Hallucinated amount rejected and replaced with safe fallback
    tampered = "Your refund of ₹9,999.00 has been processed."
    checked = verify_reply(tampered, {249900})
    assert "passed your case to a human specialist" in checked
    assert "₹9,999.00" not in checked
