"""Configuration management for ApexResolve."""
import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./apex_support.db")
    )
    payment_url: str = field(
        default_factory=lambda: os.getenv("PAYMENT_GATEWAY_URL", "http://127.0.0.1:8001")
    )
    shipping_url: str = field(
        default_factory=lambda: os.getenv("SHIPPING_GATEWAY_URL", "http://127.0.0.1:8002")
    )
    jwt_secret: str = field(
        default_factory=lambda: os.getenv("JWT_SECRET", "apex_resolve_secure_secret_key_2026")
    )
    approval_ttl_hours: int = field(
        default_factory=lambda: int(os.getenv("APPROVAL_TTL_HOURS", "24"))
    )
    max_revisions: int = field(
        default_factory=lambda: int(os.getenv("MAX_REVISIONS", "2"))
    )
    dev_mode: bool = field(
        default_factory=lambda: os.getenv("DEV_MODE", "1") in ("1", "true", "True")
    )
    shipping_down: bool = field(
        default_factory=lambda: os.getenv("SHIPPING_DOWN", "0") in ("1", "true", "True")
    )
    default_currency: str = field(
        default_factory=lambda: os.getenv("DEFAULT_CURRENCY", "USD")
    )
    auto_approve_threshold_minor: int = field(
        default_factory=lambda: int(os.getenv("AUTO_APPROVE_THRESHOLD_MINOR", "0"))
    )
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "none")
    )
    classifier_model: str = field(
        default_factory=lambda: os.getenv("CLASSIFIER_MODEL", "claude-3-5-haiku-20241022")
    )
