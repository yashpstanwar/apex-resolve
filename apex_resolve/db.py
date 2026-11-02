"""Database schema and connection factory for ApexResolve."""
from datetime import datetime, timezone
import json
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    """Return timezone-naive UTC datetime for consistent database storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(32), default="standard")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))  # confirmed, shipped, delivered, cancelled
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    paid_amount_minor: Mapped[int] = mapped_column(Integer)  # cents / paise
    item_name: Mapped[str] = mapped_column(String(256), default="Electronics Product")
    item_sku: Mapped[str] = mapped_column(String(64), default="SKU-GEN-001")
    ordered_at: Mapped[datetime] = mapped_column(DateTime)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Shipment(Base):
    __tablename__ = "shipments"

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    carrier: Mapped[str] = mapped_column(String(64), default="ApexExpress")
    tracking_number: Mapped[str] = mapped_column(String(64), default="TRK-10001")
    status: Mapped[str] = mapped_column(String(64))  # processing, in_transit, out_for_delivery, delivered, returned
    estimated_delivery: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_update_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    detail: Mapped[str] = mapped_column(Text)
    checkpoints: Mapped[list] = mapped_column(JSON, default=list)


class ReturnRecord(Base):
    __tablename__ = "returns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    rma_number: Mapped[str] = mapped_column(String(64), default="RMA-000")
    status: Mapped[str] = mapped_column(String(32))  # initiated, in_transit, received, rejected
    return_reason: Mapped[str] = mapped_column(String(128), default="customer_request")
    initiated_at: Mapped[datetime] = mapped_column(DateTime)
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    inspection_status: Mapped[str] = mapped_column(String(32), default="pending")  # pending, passed, failed
    condition_notes: Mapped[str] = mapped_column(Text, default="")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")
    priority: Mapped[str] = mapped_column(String(32), default="normal")
    summary: Mapped[str] = mapped_column(Text, default="")
    public_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    outcome: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    final_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pending: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # refund, damaged, replacement, warranty
    version: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[datetime] = mapped_column(DateTime)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    sections: Mapped[list] = mapped_column(JSON, default=list)


class ActionProposal(Base):
    __tablename__ = "action_proposals"
    __table_args__ = (UniqueConstraint("run_id", "revision", name="uq_apex_proposal_run_rev"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    action_type: Mapped[str] = mapped_column(String(32), default="refund")  # refund, replacement
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending_approval")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("action_proposals.id"), unique=True)
    reviewer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    decision: Mapped[str] = mapped_column(String(32), default="pending")  # pending, approved, rejected, changes_requested
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class RefundOperation(Base):
    __tablename__ = "refund_operations"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_apex_refund_amount_positive"),
        UniqueConstraint("proposal_id", name="uq_apex_refund_proposal"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    proposal_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    provider_reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # reserved, submitted, succeeded, failed, unknown
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReplacementOrder(Base):
    __tablename__ = "replacement_orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    original_order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    proposal_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    item_sku: Mapped[str] = mapped_column(String(64))
    item_name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="dispatched")
    tracking_number: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    actor: Mapped[str] = mapped_column(String(64))  # customer, agent, system, reviewer:X
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def make_engine(url: str):
    """Build SQLAlchemy engine with SQLite concurrency locks or Postgres pooling."""
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _connect(dbapi_conn, _):
            # Disable automatic transactions so we control BEGIN explicitly
            dbapi_conn.isolation_level = None

        @event.listens_for(engine, "begin")
        def _begin(conn):
            # BEGIN IMMEDIATE locks the database for writing immediately, avoiding write conflicts
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine

    return create_engine(url, pool_pre_ping=True)


def make_session_factory(url: str):
    """Create all tables and return an SQLAlchemy sessionmaker."""
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def audit(s, run_id: Optional[str], actor: str, event_type: str, payload: Optional[dict] = None):
    """Record an immutable audit log entry."""
    s.add(AuditEvent(run_id=run_id, actor=actor, event_type=event_type, payload=payload or {}))
