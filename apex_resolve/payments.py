"""Payment gateway client integration with fault-tolerant status mapping."""
from typing import Optional, Dict, Any
import httpx


class ProviderUncertain(Exception):
    """Network failure, timeout, or 5xx server error: refund outcome unknown, requires key reconciliation."""


class ProviderRejected(Exception):
    """Deterministic 4xx rejection: payment gateway declined the transaction payload."""


class PaymentClient:
    """HTTP client communicating with external or mock payment processor."""

    def __init__(self, base_url: str, client: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(base_url=self.base_url, timeout=5.0)

    def create_refund(self, key: str, order_id: str, amount_minor: int) -> Dict[str, Any]:
        """Dispatch an idempotent refund request to the payment gateway."""
        try:
            response = self.http.post(
                "/refunds",
                json={
                    "idempotency_key": key,
                    "order_id": order_id,
                    "amount_minor": amount_minor,
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUncertain(f"Network transport error: {exc}") from exc

        if response.status_code >= 500:
            raise ProviderUncertain(f"Payment gateway returned 5xx status: {response.status_code}")
        if response.status_code >= 400:
            raise ProviderRejected(f"Payment gateway rejected request: {response.status_code} - {response.text}")

        return response.json()

    def get_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        """Query status of a prior refund by idempotency key."""
        try:
            response = self.http.get(f"/refunds/by-key/{key}")
        except httpx.HTTPError as exc:
            raise ProviderUncertain(f"Network transport error: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ProviderUncertain(f"Payment gateway query failed: {response.status_code}")

        return response.json()
