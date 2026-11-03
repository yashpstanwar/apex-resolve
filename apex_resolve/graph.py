"""ApexResolve LangGraph Workflow Definition.

Model proposes; deterministic rules decide and execute.
Nodes:
  classify -> lookup_order -> (order_status | assess_refund | damaged_intake)
  refund:  assess -> propose -> approval_gate (interrupt) -> execute -> reply
  damaged: intake -> interrupt for missing info -> propose -> gate -> execute
  review:  reviewer can request changes -> customer interrupt -> new revision proposal
"""
import os
import re
import uuid
from datetime import timedelta
from typing import TypedDict, Optional, List, Dict, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .db import (
    ActionProposal,
    Approval,
    Order,
    Shipment,
    Ticket,
    WorkflowRun,
    audit,
    utcnow,
)
from .eligibility import Verdict
from .policies import cite, find_policy
from .refunds import RefundBlocked, execute_approved_refund, payload_hash
from .replies import compose_reply, verify_reply
from .service import assess_order_refund, get_owned_order

ORDER_REGEX = re.compile(r"ord_\d+", re.IGNORECASE)


class State(TypedDict, total=False):
    run_id: str
    customer_id: str
    message: str
    order_id: Optional[str]
    category: str
    details: Dict[str, Any]
    verdict: str
    reason_code: str
    amount_minor: int
    currency: str
    detail: Dict[str, Any]
    policy_refs: List[str]
    shipment: Dict[str, Any]
    proposal_id: str
    execution: Dict[str, Any]
    outcome: str
    reply: str
    revision: int
    feedback: str


def classify_text(message: str, settings: Any) -> str:
    """Classify user intent using LLM if configured, otherwise high-precision semantic keywords."""
    llm_cat = _try_llm_classify(message, settings)
    if llm_cat:
        return llm_cat

    lowered = message.lower()

    # Damaged item patterns
    if any(k in lowered for k in ("damaged", "broken", "cracked", "defective", "not working", "shattered")):
        return "damaged_item"

    # Refund / returns patterns
    if any(k in lowered for k in ("refund", "money back", "returned", "charged", "reimburse")):
        return "refund_request"

    # Order tracking / logistics patterns
    if any(k in lowered for k in ("where", "status", "track", "delivery", "shipped", "arrive", "parcel")):
        return "order_status"

    return "other"


def _try_llm_classify(message: str, settings: Any) -> Optional[str]:
    """Optional LLM classifier for semantic intent."""
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        if os.getenv("ANTHROPIC_API_KEY"):
            import anthropic
            client = anthropic.Anthropic()
            response = client.messages.create(
                model=getattr(settings, "classifier_model", "claude-3-5-haiku-20241022"),
                max_tokens=15,
                system="Classify the customer message as strictly one of: order_status, refund_request, "
                       "damaged_item, other. Return only the single category string.",
                messages=[{"role": "user", "content": message}],
            )
            cat = response.content[0].text.strip().lower()
            if cat in ("order_status", "refund_request", "damaged_item", "other"):
                return cat
    except Exception:
        pass

    return None


def build_graph(deps, checkpointer):
    """Construct and compile the state graph with dependencies and checkpointer."""
    Session = deps.Session
    pay = deps.pay
    settings = deps.settings

    def classify_node(state: State):
        oid = state.get("order_id")
        if not oid:
            found = ORDER_REGEX.search(state.get("message", ""))
            if found:
                oid = found.group(0).lower()

        if not oid:
            from sqlalchemy import select
            with Session() as s:
                orders = s.scalars(
                    select(Order.id).where(Order.customer_id == state["customer_id"])
                ).all()
            if len(orders) == 1:
                oid = orders[0]

        category = classify_text(state.get("message", ""), settings)
        return {"category": category, "order_id": oid}

    def lookup_order_node(state: State):
        if not state.get("order_id"):
            return {"outcome": "needs_order_id"}

        with Session.begin() as s:
            order = get_owned_order(s, state["customer_id"], state["order_id"])
            audit(
                s,
                state["run_id"],
                "agent",
                "order_lookup",
                {
                    "order_id": state["order_id"],
                    "found": order is not None,
                    "category": state["category"],
                },
            )

        if order is None:
            return {"outcome": "order_not_found"}

        return {"currency": order.currency}

    def route_after_lookup(state: State):
        if state.get("outcome"):
            return "reply"
        cat = state.get("category")
        if cat == "order_status":
            return "order_status"
        if cat == "refund_request":
            return "assess_refund"
        if cat == "damaged_item":
            return "damaged_intake"
        return "escalate"

    def order_status_node(state: State):
        if settings.shipping_down:
            return {"outcome": "order_status_unavailable"}

        with Session.begin() as s:
            sh = s.get(Shipment, state["order_id"])
            if sh is None:
                return {"outcome": "order_status_unavailable"}

            audit(
                s,
                state["run_id"],
                "agent",
                "shipment_fetched",
                {"status": sh.status, "carrier": sh.carrier},
            )
            update_str = sh.last_update_at.strftime("%Y-%m-%d %H:%M UTC") if sh.last_update_at else "recently"
            return {
                "outcome": "order_status",
                "shipment": {
                    "status": sh.status,
                    "carrier": sh.carrier,
                    "last_update_at": update_str,
                    "detail": sh.detail,
                },
            }

    def assess_refund_node(state: State):
        with Session.begin() as s:
            order = get_owned_order(s, state["customer_id"], state["order_id"])
            assessment, doc = assess_order_refund(s, order)
            audit(
                s,
                state["run_id"],
                "system",
                "eligibility_assessed",
                {
                    "verdict": assessment.verdict.value,
                    "reason": assessment.reason_code,
                    "amount_minor": assessment.amount_minor,
                },
            )
            refs = [cite(doc, assessment.reason_code)] if doc else []

        return {
            "verdict": assessment.verdict.value,
            "reason_code": assessment.reason_code,
            "amount_minor": assessment.amount_minor,
            "currency": order.currency,
            "detail": assessment.detail,
            "policy_refs": refs,
        }

    def route_after_assess(state: State):
        v = state.get("verdict")
        if v == "eligible_new_refund":
            return "propose"
        if v == "needs_review":
            return "escalate"
        return "verdict_reply"

    def verdict_reply_node(state: State):
        v = state.get("verdict")
        mapped = {
            "already_processing": "already_processing",
            "already_refunded": "already_refunded",
            "blocked": "blocked",
        }.get(v, "escalated")
        return {"outcome": mapped}

    def damaged_intake_node(state: State):
        with Session() as s:
            doc = find_policy(s, "damaged", utcnow())
            order = get_owned_order(s, state["customer_id"], state["order_id"])
            required = doc.meta.get("required_info", ["damage_description", "photo_reference"]) if doc else ["damage_description", "photo_reference"]
            window = doc.meta.get("window_days", 7) if doc else 7
            within = (
                order.delivered_at is not None
                and (utcnow() - order.delivered_at <= timedelta(days=window))
            )

        details = dict(state.get("details") or {})
        missing = [k for k in required if not details.get(k)]

        if missing:
            answer = interrupt(
                {
                    "type": "need_info",
                    "missing": missing,
                    "question": f"To process your damage claim we need: {', '.join(missing)}.",
                }
            )
            details.update(
                {k: v for k, v in (answer or {}).items() if k in required and v}
            )
            missing = [k for k in required if not details.get(k)]

        if missing or not within:
            return {
                "details": details,
                "verdict": "needs_review",
                "reason_code": "MISSING_INFO" if missing else "DAMAGE_OUTSIDE_WINDOW",
            }

        return {
            "details": details,
            "verdict": "eligible_new_refund",
            "reason_code": "DAMAGED_ITEM",
            "amount_minor": order.paid_amount_minor,
            "currency": order.currency,
            "policy_refs": [cite(doc, "DAMAGED_ITEM")],
        }

    def propose_node(state: State):
        with Session.begin() as s:
            rev = state.get("revision", 0)
            existing = (
                s.query(ActionProposal)
                .filter_by(run_id=state["run_id"], revision=rev)
                .first()
            )
            if existing:
                return {"proposal_id": existing.id}

            order = get_owned_order(s, state["customer_id"], state["order_id"])
            payload = {
                "order_id": order.id,
                "amount_minor": state["amount_minor"],
                "currency": order.currency,
                "reason_code": state["reason_code"],
                "policy_refs": state.get("policy_refs", []),
                "evidence": state.get("details") or {},
                "revision": rev,
            }
            if rev:
                payload["reviewer_feedback"] = state.get("feedback", "")

            pid = f"prop_{uuid.uuid4().hex[:12]}"
            proposal = ActionProposal(
                id=pid,
                run_id=state["run_id"],
                revision=rev,
                action_type="refund",
                payload=payload,
                payload_hash=payload_hash(payload),
            )
            s.add(proposal)
            s.flush()

            approval = Approval(
                id=f"appr_{uuid.uuid4().hex[:12]}",
                proposal_id=pid,
                expires_at=utcnow() + timedelta(hours=settings.approval_ttl_hours),
            )
            s.add(approval)
            audit(
                s,
                state["run_id"],
                "agent",
                "proposal_created",
                {"proposal_id": pid, "payload": payload},
            )

        return {"proposal_id": pid}

    def approval_gate_node(state: State):
        # Pause run and register interrupt for human reviewer
        interrupt({"type": "approval", "proposal_id": state["proposal_id"]})
        return {}

    def route_after_gate(state: State):
        with Session() as s:
            appr = (
                s.query(Approval).filter_by(proposal_id=state["proposal_id"]).one()
            )
            decision = appr.decision
        return "request_changes" if decision == "changes_requested" else "execute"

    def request_changes_node(state: State):
        rev = state.get("revision", 0)
        with Session() as s:
            appr = s.query(Approval).filter_by(proposal_id=state["proposal_id"]).one()
            notes = appr.notes or ""

        if rev + 1 > settings.max_revisions:
            return {"outcome": "escalated", "reason_code": "TOO_MANY_REVISIONS"}

        answer = interrupt(
            {
                "type": "need_info",
                "missing": ["additional_info"],
                "question": f"A reviewer needs more information before approving: {notes}",
            }
        )
        text = ((answer or {}).get("additional_info") or "").strip()
        if not text:
            return {"outcome": "escalated", "reason_code": "NO_ADDITIONAL_INFO"}

        details = dict(state.get("details") or {})
        details["additional_info"] = text
        with Session.begin() as s:
            audit(
                s,
                state["run_id"],
                "customer",
                "additional_info_provided",
                {"revision": rev + 1},
            )

        return {"revision": rev + 1, "details": details, "feedback": notes}

    def route_after_changes(state: State):
        return "reply" if state.get("outcome") else "propose"

    def execute_node(state: State):
        with Session() as s:
            appr = s.query(Approval).filter_by(proposal_id=state["proposal_id"]).one()
            decision = appr.decision

        if decision == "rejected":
            return {"outcome": "rejected"}

        try:
            res = execute_approved_refund(Session, state["proposal_id"], pay)
        except RefundBlocked as exc:
            outcome = (
                "approval_expired"
                if exc.code == "APPROVAL_EXPIRED"
                else "revalidation_failed"
            )
            return {"outcome": outcome, "reason_code": exc.code}

        status_to_outcome = {
            "succeeded": "refund_succeeded",
            "submitted": "refund_submitted",
            "unknown": "refund_unknown",
            "failed": "refund_failed",
            "reserved": "refund_unknown",
        }
        out = status_to_outcome.get(res.get("status"), "refund_unknown")
        return {"execution": res, "outcome": out}

    def escalate_node(state: State):
        return {"outcome": "escalated"}

    def reply_node(state: State):
        outcome = state.get("outcome", "escalated")
        reply_text = compose_reply(outcome, dict(state))

        allowed_amounts = {
            state.get("amount_minor", 0),
            (state.get("detail") or {}).get("op", {}).get("amount_minor", 0),
            (state.get("execution") or {}).get("amount_minor", 0),
        }
        verified_text = verify_reply(reply_text, allowed_amounts)

        ticket_status_map = {
            "escalated": "escalated",
            "rejected": "escalated",
            "refund_failed": "escalated",
            "revalidation_failed": "escalated",
            "approval_expired": "escalated",
            "refund_unknown": "pending_reconciliation",
            "refund_submitted": "pending_provider",
            "needs_order_id": "waiting_customer",
        }
        ticket_status = ticket_status_map.get(outcome, "resolved")

        with Session.begin() as s:
            ticket = (
                s.query(Ticket)
                .join(WorkflowRun, WorkflowRun.ticket_id == Ticket.id)
                .filter(WorkflowRun.id == state["run_id"])
                .one()
            )
            ticket.status = ticket_status
            ticket.public_note = verified_text
            ticket.category = state.get("category")
            audit(
                s,
                state["run_id"],
                "agent",
                "reply_sent",
                {"outcome": outcome, "ticket_status": ticket_status},
            )

        return {"reply": verified_text}

    # Assemble State Graph
    graph = StateGraph(State)
    graph.add_node("classify", classify_node)
    graph.add_node("lookup_order", lookup_order_node)
    graph.add_node("order_status", order_status_node)
    graph.add_node("assess_refund", assess_refund_node)
    graph.add_node("verdict_reply", verdict_reply_node)
    graph.add_node("damaged_intake", damaged_intake_node)
    graph.add_node("propose", propose_node)
    graph.add_node("approval_gate", approval_gate_node)
    graph.add_node("execute", execute_node)
    graph.add_node("request_changes", request_changes_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("reply", reply_node)

    graph.add_edge(START, "classify")
    graph.add_edge("classify", "lookup_order")
    graph.add_conditional_edges(
        "lookup_order",
        route_after_lookup,
        ["reply", "order_status", "assess_refund", "damaged_intake", "escalate"],
    )
    graph.add_edge("order_status", "reply")
    graph.add_conditional_edges(
        "assess_refund",
        route_after_assess,
        ["propose", "escalate", "verdict_reply"],
    )
    graph.add_edge("verdict_reply", "reply")
    graph.add_conditional_edges(
        "damaged_intake",
        lambda s: "propose" if s.get("verdict") == "eligible_new_refund" else "escalate",
        ["propose", "escalate"],
    )
    graph.add_edge("propose", "approval_gate")
    graph.add_conditional_edges(
        "approval_gate",
        route_after_gate,
        ["execute", "request_changes"],
    )
    graph.add_conditional_edges(
        "request_changes",
        route_after_changes,
        ["reply", "propose"],
    )
    graph.add_edge("execute", "reply")
    graph.add_edge("escalate", "reply")
    graph.add_edge("reply", END)

    return graph.compile(checkpointer=checkpointer)
