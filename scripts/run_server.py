"""Combined runner that boots both the Mock Payment Gateway and the ApexResolve Engine.

Usage:
    python -m scripts.run_server
"""
import sys
import threading
import time
import uvicorn

from apex_resolve.config import Settings
from apex_resolve.main import create_app
from apex_resolve.payments import PaymentClient
from mock_gateways.payments import app as payment_app

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_payment_gateway():
    uvicorn.run(payment_app, host="127.0.0.1", port=8001, log_level="warning")


def main():
    print("=" * 70)
    print("  Booting ApexResolve Full-Stack Local Cluster")
    print("=" * 70)

    # 1. Start Payment Gateway on port 8001
    print("[1/2] Starting Mock Payment Gateway at http://127.0.0.1:8001 ...")
    pay_thread = threading.Thread(target=run_payment_gateway, daemon=True)
    pay_thread.start()

    time.sleep(1)

    # 2. Start ApexResolve API on port 8000
    print("[2/2] Starting ApexResolve Engine at http://127.0.0.1:8000 ...")
    settings = Settings(
        database_url="sqlite:///./apex_support.db",
        payment_url="http://127.0.0.1:8001",
        dev_mode=True,
    )
    app = create_app(settings, PaymentClient("http://127.0.0.1:8001"), seed_data=True)

    print("\n" + "*" * 70)
    print("  🚀 ApexResolve is live and ready for interactions!")
    print("  👉 Web Console:   http://127.0.0.1:8000/ui")
    print("  👉 Swagger Docs:  http://127.0.0.1:8000/docs")
    print("  👉 Health Check:  http://127.0.0.1:8000/health")
    print("  👉 Gateway Admin: http://127.0.0.1:8001/admin/refunds")
    print("*" * 70 + "\n")

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
