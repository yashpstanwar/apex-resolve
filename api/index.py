"""Vercel Serverless Entrypoint for ApexResolve.

Provides seamless serverless compatibility on Vercel:
- Automatically uses /tmp ephemeral database storage on Vercel
- Embeds in-memory mock payment processor fallback if external gateway URL is omitted
- Exposes root endpoint '/' and '/ui' dashboard
"""
import os
import tempfile
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from apex_resolve.config import Settings
from apex_resolve.main import create_app
from apex_resolve.payments import PaymentClient
from mock_gateways.payments import app as mock_payment_gateway_app

# 1. Configure serverless environment settings
db_url = os.getenv("DATABASE_URL")
if not db_url:
    # Cross-platform temp directory (writable on Vercel /tmp, Windows, Linux, and macOS)
    t = tempfile.gettempdir().replace("\\", "/")
    db_url = f"sqlite:///{t}/apex_support.db"

settings = Settings(
    database_url=db_url,
    payment_url=os.getenv("PAYMENT_GATEWAY_URL", "http://mock-payment"),
    jwt_secret=os.getenv("JWT_SECRET", "apex_resolve_secure_secret_key_2026"),
    dev_mode=True,
)

# 2. Resilient payment client: if no external gateway URL is provided,
# use in-memory TestClient so the entire multi-agent loop works serverlessly on Vercel
if os.getenv("PAYMENT_GATEWAY_URL"):
    pay_client = PaymentClient(os.getenv("PAYMENT_GATEWAY_URL"))
else:
    pay_http = TestClient(mock_payment_gateway_app)
    pay_client = PaymentClient("http://mock-payment", client=pay_http)

# 3. Instantiate the FastAPI application
app = create_app(settings, pay_client, seed_data=True)
