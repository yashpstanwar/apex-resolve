"""Concurrency and Idempotency tests for ApexResolve."""
import concurrent.futures
from apex_resolve.db import ActionProposal, Approval, Order, RefundOperation, utcnow
from apex_resolve.refunds import execute_approved_refund, payload_hash, idem_key


def test_duplicate_execute_does_not_duplicate_refunds(env):
    Session = env["app"].state.deps.Session
    pay = env["pay_client"]

    with Session.begin() as s:
        p_data = {"order_id": "ord_1003", "amount_minor": 249900, "reason_code": "RETURN_RECEIVED_INSPECTION_PASSED"}
        prop = ActionProposal(id="prop_conc_1", run_id="run_c1", revision=0, payload=p_data, payload_hash=payload_hash(p_data))
        s.add(prop)
        s.flush()
        from datetime import timedelta
        appr = Approval(id="appr_c1", proposal_id=prop.id, decision="approved", expires_at=utcnow() + timedelta(hours=24))
        s.add(appr)

    # First execution creates the operation
    r1 = execute_approved_refund(Session, "prop_conc_1", pay)
    assert r1["created"] is True
    assert r1["status"] in ("succeeded", "submitted")

    # Second execution returns the existing operation with created=False
    r2 = execute_approved_refund(Session, "prop_conc_1", pay)
    assert r2["created"] is False
    assert r2["id"] == r1["id"]

    # In database, exactly one operation exists
    with Session() as s:
        from sqlalchemy import select
        ops = s.scalars(select(RefundOperation).where(RefundOperation.proposal_id == "prop_conc_1")).all()
        assert len(ops) == 1


def test_concurrent_execution_race_condition(env):
    Session = env["app"].state.deps.Session
    pay = env["pay_client"]

    with Session.begin() as s:
        p_data = {"order_id": "ord_1003", "amount_minor": 249900, "reason_code": "RETURN_RECEIVED_INSPECTION_PASSED"}
        prop = ActionProposal(id="prop_race", run_id="run_race", revision=0, payload=p_data, payload_hash=payload_hash(p_data))
        s.add(prop)
        s.flush()
        from datetime import timedelta
        appr = Approval(id="appr_race", proposal_id=prop.id, decision="approved", expires_at=utcnow() + timedelta(hours=24))
        s.add(appr)

    # Fire 5 concurrent executions of the exact same approved proposal
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(execute_approved_refund, Session, "prop_race", pay) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    created_count = sum(1 for r in results if r.get("created") is True)
    assert created_count == 1, f"Expected exactly 1 creation, got {created_count}"

    # Verify provider and DB state
    with Session() as s:
        from sqlalchemy import select
        ops = s.scalars(select(RefundOperation).where(RefundOperation.proposal_id == "prop_race")).all()
        assert len(ops) == 1
