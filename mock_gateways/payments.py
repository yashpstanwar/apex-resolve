"""Mock Payment Processor Gateway with strict Idempotency Contracts and failure injection.

Simulates:
- Idempotent API contract (same key & payload -> identical response; key reuse with changed payload -> 409 Conflict)
- Deterministic failure modes:
  * error_500: simulated server failure prior to reservation
  * slow: network throttling / latency
  * timeout_after_accept: transaction accepted & persisted by gateway, but response connection drops (504)
"""
import threading
import time
import uuid
from typing import Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="ApexResolve Mock Payment Gateway")

LOCK = threading.Lock()
GATEWAY_STATE: Dict[str, Any] = {
    "mode": "normal",
    "times": 0,
    "refunds": {},
}


class RefundRequest(BaseModel):
    idempotency_key: str
    order_id: str
    amount_minor: int


class ModeRequest(BaseModel):
    mode: str
    times: int = 1


def _consume_mode() -> str:
    if GATEWAY_STATE["times"] > 0:
        GATEWAY_STATE["times"] -= 1
        return GATEWAY_STATE["mode"]
    return "normal"


@app.post("/refunds")
def create_refund(req: RefundRequest):
    with LOCK:
        active_mode = _consume_mode()

    if active_mode == "error_500":
        raise HTTPException(status_code=500, detail="Payment gateway internal server error")

    if active_mode == "slow":
        time.sleep(1.5)

    with LOCK:
        existing = GATEWAY_STATE["refunds"].get(req.idempotency_key)
        if existing:
            # Enforce idempotency contract: payload mismatch triggers 409
            if (existing["order_id"], existing["amount_minor"]) != (req.order_id, req.amount_minor):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key reused with mismatched transaction payload",
                )
            record = existing
        else:
            record = {
                "refund_id": f"re_{uuid.uuid4().hex[:10]}",
                "idempotency_key": req.idempotency_key,
                "order_id": req.order_id,
                "amount_minor": req.amount_minor,
                "status": "pending",
            }
            GATEWAY_STATE["refunds"][req.idempotency_key] = record

    if active_mode == "timeout_after_accept":
        raise HTTPException(
            status_code=504,
            detail="Gateway timeout (transaction was committed at gateway, but downstream connection dropped)",
        )

    return record


@app.get("/refunds/by-key/{key}")
def get_by_key(key: str):
    record = GATEWAY_STATE["refunds"].get(key)
    if not record:
        raise HTTPException(status_code=404, detail="Refund record not found")
    return record


@app.get("/admin/refunds")
def list_refunds():
    return list(GATEWAY_STATE["refunds"].values())


@app.post("/admin/mode")
def set_mode(req: ModeRequest):
    GATEWAY_STATE["mode"] = req.mode
    GATEWAY_STATE["times"] = req.times
    return GATEWAY_STATE["mode"]


@app.post("/admin/settle/{refund_id}")
def settle(refund_id: str):
    for r in GATEWAY_STATE["refunds"].values():
        if r["refund_id"] == refund_id:
            r["status"] = "succeeded"
            return r
    raise HTTPException(status_code=404, detail="Refund not found")


@app.post("/admin/reset")
def reset():
    GATEWAY_STATE.update(mode="normal", times=0, refunds={})
    return {"status": "ok"}
