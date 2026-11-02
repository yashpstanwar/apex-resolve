"""Mock Shipping & Carrier Logistics Gateway."""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="ApexResolve Mock Logistics Gateway")

CARRIER_STATUSES = {
    "ord_1001": {"status": "delivered", "location": "Customer Address", "detail": "Signed by resident"},
    "ord_1002": {"status": "in_transit", "location": "Bhubaneswar Hub", "detail": "Departed transit hub"},
}


@app.get("/track/{order_id}")
def track_shipment(order_id: str):
    info = CARRIER_STATUSES.get(order_id)
    if not info:
        return {"order_id": order_id, "status": "processing", "detail": "Carrier received dispatch notification"}
    return {"order_id": order_id, **info}
