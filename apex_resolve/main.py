"""FastAPI Application for ApexResolve - Customer Support & Settlement Engine."""
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel
from sqlalchemy import select

from .config import Settings
from .db import (
    ActionProposal,
    Approval,
    AuditEvent,
    Customer,
    Ticket,
    WorkflowRun,
    audit,
    make_session_factory,
    utcnow,
)
from .graph import build_graph
from .payments import PaymentClient
from .refunds import reconcile
from .seed import seed


@dataclass
class Deps:
    Session: Any
    pay: PaymentClient
    settings: Settings


def make_checkpointer(settings: Settings):
    """Provide durable checkpointer when database or sqlite path is supplied."""
    if settings.database_url.startswith("postgresql"):
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            conn_str = settings.database_url.replace("+psycopg", "")
            cm = PostgresSaver.from_conn_string(conn_str)
            saver = cm.__enter__()
            saver.setup()
            return saver, lambda: cm.__exit__(None, None, None)
        except ImportError:
            pass

    if settings.database_url.startswith("sqlite:///") and ":memory:" not in settings.database_url:
        try:
            import sqlite3
            from langgraph.checkpoint.sqlite import SqliteSaver

            db_path = Path(settings.database_url[len("sqlite:///"):]).with_suffix(".checkpoints.db")
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            return saver, conn.close
        except (ImportError, Exception):
            pass

    return MemorySaver(), lambda: None


def create_app(
    settings: Optional[Settings] = None,
    pay: Optional[PaymentClient] = None,
    seed_data: bool = True,
) -> FastAPI:
    """Build and configure the production FastAPI application."""
    settings = settings or Settings()
    Session = make_session_factory(settings.database_url)

    if seed_data:
        with Session.begin() as s:
            if s.scalars(select(Customer)).first() is None:
                seed(s)

    deps = Deps(Session, pay or PaymentClient(settings.payment_url), settings)
    checkpointer, closer = make_checkpointer(settings)
    graph = build_graph(deps, checkpointer)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        closer()

    app = FastAPI(
        title="ApexResolve Support & Financial Settlement Engine",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.deps = deps
    app.state.graph = graph

    # Authentication dependencies
    def user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization[7:]
        try:
            return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="invalid token")

    def reviewer(u=Depends(user)) -> Dict[str, Any]:
        if u.get("role") != "reviewer":
            raise HTTPException(status_code=403, detail="reviewer role required")
        return u

    class TokenRequest(BaseModel):
        sub: str
        role: str = "customer"

    @app.post("/dev/token")
    def dev_token(req: TokenRequest):
        if not settings.dev_mode:
            raise HTTPException(status_code=404, detail="dev tokens disabled")
        exp = datetime.now(timezone.utc) + timedelta(hours=12)
        payload = {"sub": req.sub, "role": req.role, "exp": exp}
        return {"token": jwt.encode(payload, settings.jwt_secret, algorithm="HS256")}

    @app.get("/", response_class=HTMLResponse)
    def root_index():
        """Root view renders the unified web console."""
        index_file = Path(__file__).parent / "static" / "index.html"
        return index_file.read_text(encoding="utf-8")

    @app.get("/ui", response_class=HTMLResponse)
    def web_console():
        """Unified Customer & Reviewer Operations Console."""
        if not settings.dev_mode:
            raise HTTPException(status_code=404, detail="console disabled in production")
        index_file = Path(__file__).parent / "static" / "index.html"
        return index_file.read_text(encoding="utf-8")

    @app.get("/health")
    def health_check():
        return {"ok": True, "engine": "ApexResolve", "timestamp": utcnow().isoformat()}

    # Internal helpers
    def run_view(s, run: WorkflowRun) -> Dict[str, Any]:
        return {
            "run_id": run.id,
            "status": run.status,
            "outcome": run.outcome,
            "reply": run.final_response,
            "pending": run.pending,
            "ticket_id": run.ticket_id,
        }

    def apply_result(run_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        with Session.begin() as s:
            run = s.get(WorkflowRun, run_id)
            interrupts = result.get("__interrupt__")
            if interrupts:
                val = interrupts[0].value
                run.status = "waiting_approval" if val.get("type") == "approval" else "waiting_customer"
                run.pending = val
            else:
                run.status = "completed"
                run.pending = None
                run.outcome = result.get("outcome")
                run.final_response = result.get("reply")
                run.completed_at = utcnow()
            audit(s, run_id, "system", "run_status", {"status": run.status})
            return run_view(s, run)

    def verify_run_ownership(s, run_id: str, u: Dict[str, Any]) -> WorkflowRun:
        run = s.get(WorkflowRun, run_id)
        if run is None or (u.get("role") != "reviewer" and run.customer_id != u.get("sub")):
            raise HTTPException(status_code=404, detail="run not found")
        return run

    # Customer endpoints
    class RunRequest(BaseModel):
        message: str
        order_id: Optional[str] = None
        details: Optional[Dict[str, Any]] = None

    @app.post("/runs")
    def start_run(req: RunRequest, u=Depends(user)):
        if u.get("role") != "customer":
            raise HTTPException(status_code=403, detail="customer role required")

        with Session.begin() as s:
            if s.get(Customer, u["sub"]) is None:
                raise HTTPException(status_code=403, detail="unknown customer")

            ticket = Ticket(customer_id=u["sub"], summary=req.message[:200])
            s.add(ticket)
            s.flush()

            run_id = f"run_{uuid.uuid4().hex[:12]}"
            s.add(WorkflowRun(id=run_id, ticket_id=ticket.id, customer_id=u["sub"]))
            audit(s, run_id, "customer", "run_started", {"message": req.message[:200]})

        # Invoke workflow graph
        result = graph.invoke(
            {
                "run_id": run_id,
                "customer_id": u["sub"],
                "message": req.message,
                "order_id": req.order_id,
                "details": req.details or {},
            },
            {"configurable": {"thread_id": run_id}},
        )
        return apply_result(run_id, result)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, u=Depends(user)):
        with Session() as s:
            return run_view(s, verify_run_ownership(s, run_id, u))

    @app.get("/runs/{run_id}/events")
    def get_events(run_id: str, u=Depends(user)):
        with Session() as s:
            verify_run_ownership(s, run_id, u)
            rows = s.scalars(
                select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.id)
            ).all()
            return [
                {
                    "at": r.created_at.isoformat(),
                    "actor": r.actor,
                    "event": r.event_type,
                    "payload": r.payload if u.get("role") == "reviewer" else {},
                }
                for r in rows
            ]

    class MessageRequest(BaseModel):
        damage_description: Optional[str] = None
        additional_info: Optional[str] = None
        photo_reference: Optional[str] = None

    @app.post("/runs/{run_id}/messages")
    def submit_info(run_id: str, req: MessageRequest, u=Depends(user)):
        with Session() as s:
            run = verify_run_ownership(s, run_id, u)
            if run.status != "waiting_customer":
                raise HTTPException(status_code=409, detail="run is not waiting for customer information")

        data = req.model_dump(exclude_none=True)
        result = graph.invoke(Command(resume=data), {"configurable": {"thread_id": run_id}})
        return apply_result(run_id, result)

    # Reviewer governance endpoints
    @app.get("/approvals")
    def list_approvals(_=Depends(reviewer)):
        with Session() as s:
            rows = s.execute(
                select(Approval, ActionProposal)
                .join(ActionProposal, ActionProposal.id == Approval.proposal_id)
                .where(Approval.decision == "pending")
            ).all()
            return [
                {
                    "approval_id": a.id,
                    "proposal_id": a.proposal_id,
                    "run_id": p.run_id,
                    "expires_at": a.expires_at.isoformat(),
                    "revision": p.revision,
                    "proposal": p.payload,
                }
                for a, p in rows
            ]

    class DecisionRequest(BaseModel):
        decision: str  # approved | rejected | changes_requested
        notes: Optional[str] = None

    @app.post("/approvals/{approval_id}/decision")
    def decide(approval_id: str, req: DecisionRequest, u=Depends(reviewer)):
        if req.decision not in ("approved", "rejected", "changes_requested"):
            raise HTTPException(status_code=422, detail="decision must be approved, rejected or changes_requested")
        if req.decision == "changes_requested" and not (req.notes or "").strip():
            raise HTTPException(status_code=422, detail="notes are required when requesting changes")

        with Session.begin() as s:
            approval = s.get(Approval, approval_id)
            if approval is None:
                raise HTTPException(status_code=404, detail="approval not found")
            if approval.decision != "pending":
                raise HTTPException(status_code=409, detail=f"already {approval.decision}")
            if approval.expires_at < utcnow():
                raise HTTPException(status_code=409, detail="approval expired")

            prop = s.get(ActionProposal, approval.proposal_id)
            approval.decision = req.decision
            approval.reviewer_id = u["sub"]
            approval.notes = req.notes
            approval.decided_at = utcnow()
            audit(
                s,
                prop.run_id,
                f"reviewer:{u['sub']}",
                f"approval_{req.decision}",
                {"notes": req.notes},
            )
            run_id = prop.run_id

        result = graph.invoke(Command(resume={"approval_id": approval_id}), {"configurable": {"thread_id": run_id}})
        return apply_result(run_id, result)

    @app.get("/tickets/{ticket_id}")
    def get_ticket(ticket_id: int, u=Depends(user)):
        with Session() as s:
            t = s.get(Ticket, ticket_id)
            if t is None or (u.get("role") != "reviewer" and t.customer_id != u.get("sub")):
                raise HTTPException(status_code=404, detail="ticket not found")
            return {
                "ticket_id": t.id,
                "status": t.status,
                "category": t.category,
                "public_note": t.public_note,
            }

    @app.post("/refund-operations/{op_id}/reconcile")
    def reconcile_operation(op_id: str, _=Depends(reviewer)):
        return reconcile(Session, op_id, deps.pay)

    @app.get("/refund-operations")
    def list_operations(_=Depends(reviewer)):
        from .db import RefundOperation
        from .service import op_dict

        with Session() as s:
            return [
                op_dict(o) | {"order_id": o.order_id}
                for o in s.scalars(select(RefundOperation))
            ]

    return app


def app_factory():
    """Uvicorn entry factory."""
    return create_app()


# Module-level ASGI instance for direct serverless and ASGI runner imports
app = create_app()
