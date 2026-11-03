"""Pytest fixtures and test environment setup for ApexResolve using in-memory TestClient."""
import pytest
from fastapi.testclient import TestClient

from apex_resolve.config import Settings
from apex_resolve.main import create_app
from apex_resolve.payments import PaymentClient
from mock_gateways.payments import app as payment_gateway_app


@pytest.fixture
def env(tmp_path):
    """Provide isolated SQLite database and in-memory TestClients (zero network sockets needed)."""
    # 1. In-memory payment gateway client using TestClient
    pay_http = TestClient(payment_gateway_app)
    pay_http.post("/admin/reset")
    payment_client = PaymentClient(base_url="http://mock-payment", client=pay_http)

    # 2. ApexResolve API configured with the in-memory payment client
    db_file = tmp_path / "test.db"
    settings = Settings(
        database_url=f"sqlite:///{db_file}",
        payment_url="http://mock-payment",
        jwt_secret="apex_resolve_secure_test_jwt_secret_key_32bytes_min_super_secure",
        dev_mode=True,
    )
    app = create_app(settings, payment_client, seed_data=True)

    # 3. TestClient for ApexResolve
    client = TestClient(app)

    def get_token(sub: str, role: str = "customer") -> str:
        res = client.post("/dev/token", json={"sub": sub, "role": role})
        return res.json()["token"]

    def auth_headers(sub: str, role: str = "customer") -> dict:
        return {"Authorization": f"Bearer {get_token(sub, role)}"}

    return {
        "app": app,
        "client": client,
        "settings": settings,
        "db_file": db_file,
        "pay_client": payment_client,
        "pay_http": pay_http,
        "token": get_token,
        "headers": auth_headers,
    }
