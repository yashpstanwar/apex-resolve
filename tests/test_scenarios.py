"""End-to-end multi-agent customer support scenarios."""
import pytest


def test_scenario_order_status_delivered(env):
    client = env["client"]
    h = env["headers"]("cust_status", "customer")
    r = client.post("/runs", headers=h, json={"message": "Where is my order?"}).json()
    assert r["status"] == "completed"
    assert r["outcome"] == "order_status"
    assert "delivered" in r["reply"].lower()
    assert "signed by resident" in r["reply"].lower()


def test_scenario_order_status_in_transit(env):
    client = env["client"]
    h = env["headers"]("cust_transit", "customer")
    r = client.post("/runs", headers=h, json={"message": "Track my delivery please"}).json()
    assert r["status"] == "completed"
    assert r["outcome"] == "order_status"
    assert "Departed Bhubaneswar hub" in r["reply"]


def test_scenario_eligible_refund_full_lifecycle(env):
    client = env["client"]
    cust_h = env["headers"]("cust_eligible", "customer")
    rev_h = env["headers"]("reviewer_1", "reviewer")

    # 1. Customer initiates refund request
    r = client.post("/runs", headers=cust_h, json={"message": "I returned my headphones last week but have no refund."}).json()
    assert r["status"] == "waiting_approval"
    run_id = r["run_id"]

    # 2. Reviewer inspects approval queue
    approvals = client.get("/approvals", headers=rev_h).json()
    matching = [a for a in approvals if a["run_id"] == run_id]
    assert len(matching) == 1
    appr = matching[0]
    assert appr["proposal"]["amount_minor"] == 249900
    assert appr["proposal"]["order_id"] == "ord_1003"

    # 3. Reviewer approves proposal
    r_after = client.post(f"/approvals/{appr['approval_id']}/decision", headers=rev_h, json={"decision": "approved"}).json()
    assert r_after["status"] == "completed"
    assert r_after["outcome"] == "refund_submitted"
    assert "still processing" in r_after["reply"]


def test_scenario_damaged_item_with_interrupt_for_info(env):
    client = env["client"]
    cust_h = env["headers"]("cust_damaged", "customer")
    rev_h = env["headers"]("reviewer_1", "reviewer")

    # 1. Customer reports damaged monitor without details
    r = client.post("/runs", headers=cust_h, json={"message": "My monitor arrived cracked"}).json()
    assert r["status"] == "waiting_customer"
    assert "damage_description" in r["pending"]["missing"]
    assert "photo_reference" in r["pending"]["missing"]

    # 2. Customer provides the required evidence
    r2 = client.post(
        f"/runs/{r['run_id']}/messages",
        headers=cust_h,
        json={"damage_description": "Screen panel shattered upon unboxing", "photo_reference": "IMG_9912.JPG"},
    ).json()
    assert r2["status"] == "waiting_approval"

    # 3. Reviewer approves
    approvals = client.get("/approvals", headers=rev_h).json()
    appr = next(a for a in approvals if a["run_id"] == r["run_id"])
    assert appr["proposal"]["evidence"]["photo_reference"] == "IMG_9912.JPG"

    r_final = client.post(f"/approvals/{appr['approval_id']}/decision", headers=rev_h, json={"decision": "approved"}).json()
    assert r_final["status"] == "completed"
    assert r_final["outcome"] == "refund_submitted"


def test_scenario_reviewer_request_changes_loop(env):
    client = env["client"]
    cust_h = env["headers"]("cust_damaged_full", "customer")
    rev_h = env["headers"]("reviewer_1", "reviewer")

    # Customer files claim
    r = client.post("/runs", headers=cust_h, json={
        "message": "Screen is cracked on arrival",
        "details": {"damage_description": "cracked glass", "photo_reference": "IMG_01"},
    }).json()
    assert r["status"] == "waiting_approval"

    # Reviewer requests clarification
    appr = client.get("/approvals", headers=rev_h).json()[0]
    r_chg = client.post(
        f"/approvals/{appr['approval_id']}/decision",
        headers=rev_h,
        json={"decision": "changes_requested", "notes": "Please provide serial number from box"},
    ).json()
    assert r_chg["status"] == "waiting_customer"

    # Customer responds
    r_cust = client.post(
        f"/runs/{r['run_id']}/messages",
        headers=cust_h,
        json={"additional_info": "Serial number is SN-998822"},
    ).json()
    assert r_cust["status"] == "waiting_approval"

    # Reviewer sees revision 1 with customer feedback
    appr2 = client.get("/approvals", headers=rev_h).json()[0]
    assert appr2["revision"] == 1
    assert appr2["proposal"]["reviewer_feedback"] == "Please provide serial number from box"

    # Reviewer approves
    r_done = client.post(f"/approvals/{appr2['approval_id']}/decision", headers=rev_h, json={"decision": "approved"}).json()
    assert r_done["status"] == "completed"
    assert r_done["outcome"] == "refund_submitted"


def test_scenario_gateway_timeout_and_reconciliation(env):
    client = env["client"]
    cust_h = env["headers"]("cust_eligible", "customer")
    rev_h = env["headers"]("reviewer_1", "reviewer")

    # Configure gateway to simulate 504 Timeout after recording the refund
    env["pay_http"].post("/admin/mode", json={"mode": "timeout_after_accept", "times": 1})

    r = client.post("/runs", headers=cust_h, json={"message": "Please refund my returned headphones"}).json()
    appr = client.get("/approvals", headers=rev_h).json()[0]
    r_app = client.post(f"/approvals/{appr['approval_id']}/decision", headers=rev_h, json={"decision": "approved"}).json()

    # System recognizes uncertainty and sets refund_unknown
    assert r_app["outcome"] == "refund_unknown"
    assert "will not issue a duplicate" in r_app["reply"]

    # Exactly one refund recorded at payment gateway
    prov_refunds = env["pay_http"].get("/admin/refunds").json()
    matching = [x for x in prov_refunds if x["order_id"] == "ord_1003"]
    assert len(matching) == 1

    # Reviewer reconciles operation by key
    op = next(o for o in client.get("/refund-operations", headers=rev_h).json() if o["order_id"] == "ord_1003")
    reconciled = client.post(f"/refund-operations/{op['id']}/reconcile", headers=rev_h).json()
    assert reconciled["status"] == "submitted"
    assert reconciled["provider_reference"] is not None
