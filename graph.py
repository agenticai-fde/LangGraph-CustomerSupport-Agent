"""Customer support agent that works a ticket end to end.

load_ticket -> triage -> retrieve_kb -> plan_resolution -> check_policy -> human_approval -> execute_action -> resolve
                  |                          |                  |                |
                  +-> escalate <-------------+------------------+----------------+
                                       (plan_resolution -> resolve when no action needs approval)

Money and replacements always pass a code-level policy check and a human approval
(LangGraph interrupt) before anything is written.
"""
import json
import re
from datetime import date
from typing import Literal, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

import support_db as db
from config import get_llm

MIN_CONFIDENCE = 0.6
ESCALATE_RE = re.compile(r"lawyer|legal action|\bsue\b|court|consumer forum|cancel\w* (our|the|my)?\s*"
                         r"(enterprise )?(contract|account)|fraud|unsafe|\bfire\b|\bburn", re.I)


class Triage(BaseModel):
    category: Literal["order_status", "return_refund", "warranty", "billing", "account", "complaint", "other"]
    priority: Literal["low", "medium", "high", "urgent"]
    sentiment: Literal["positive", "neutral", "negative", "angry"]
    needs_human: bool = Field(description="True only if policy says a human must handle it")
    reason: str = Field(description="One sentence explaining the triage")


class Resolution(BaseModel):
    reply: str = Field(description="Email reply to the customer, 3-6 sentences, signed 'ShopEase Support'")
    action: Literal["none", "refund", "replacement", "return_pickup", "escalate"]
    order_number: Optional[str] = None
    refund_amount: Optional[float] = Field(None, description="INR, only when action is refund")
    confidence: float = Field(description="0 to 1: how sure you are this fully resolves the ticket")
    internal_note: str = Field(description="One-line note for the support team")


class TicketState(TypedDict, total=False):
    ticket_id: int
    ticket: dict
    orders: list[dict]
    triage: dict
    kb: list[dict]
    plan: dict
    policy_issue: Optional[str]
    approval: dict
    outcome: str
    escalation_reason: str


# ---------- Nodes ----------

def load_ticket(state: TicketState) -> dict:
    ticket = db.get_ticket(state["ticket_id"])
    if not ticket:
        raise ValueError(f"Ticket {state['ticket_id']} not found")
    db.log_event(ticket["id"], "agent_started")
    return {"ticket": ticket, "orders": db.get_orders(ticket["customer_id"])}


def triage(state: TicketState) -> dict:
    t = state["ticket"]
    result = get_llm().with_structured_output(Triage).invoke(
        "Triage this customer support ticket.\n"
        f"Customer plan: {t['customers']['plan']}\nSubject: {t['subject']}\nMessage: {t['body']}"
    )
    data = result.model_dump()
    # Deterministic rules on top of the model's judgement.
    if ESCALATE_RE.search(f"{t['subject']} {t['body']}"):
        data.update(needs_human=True, priority="urgent",
                    reason=data["reason"] + " (legal/contract/safety keywords detected)")
    if t["customers"]["plan"] == "enterprise" and data["priority"] in ("low", "medium"):
        data["priority"] = "high"

    db.update_ticket(t["id"], category=data["category"], priority=data["priority"],
                     sentiment=data["sentiment"])
    db.log_event(t["id"], "triaged", data)
    return {"triage": data}


def retrieve_kb(state: TicketState) -> dict:
    t = state["ticket"]
    articles = db.search_kb(f"{state['triage']['category'].replace('_', ' ')} {t['subject']} {t['body']}")
    db.log_event(t["id"], "kb_retrieved", {"articles": [a["title"] for a in articles]})
    return {"kb": articles}


def plan_resolution(state: TicketState) -> dict:
    t, orders = state["ticket"], state["orders"]
    order_lines = "\n".join(
        f"- {o['order_number']}: {o['quantity']} x {o['products']['name']}, Rs {o['total']}, "
        f"{o['status']}, ordered {o['order_date']}" for o in orders) or "(no orders)"
    kb = "\n\n".join(f"[{a['title']}] {a['content']}" for a in state["kb"]) or "(no articles)"
    result = get_llm().with_structured_output(Resolution).invoke(
        f"""You are a ShopEase support agent. Today is {date.today().isoformat()}.
Resolve the ticket following ONLY the knowledge base. Choose an action:
- none: an informational answer fully resolves it
- return_pickup: an eligible return; tell the customer a pickup label is on its way
- replacement: a defect covered by warranty
- refund: money must be returned (set order_number and refund_amount)
- escalate: policy doesn't cover it or a human must decide
Never promise anything the knowledge base doesn't allow.

CUSTOMER: {t['customers']['name']} ({t['customers']['plan']} plan, {t['customers']['email']})
TICKET #{t['id']} - {t['subject']}
{t['body']}
Referenced order: {t['order_number'] or 'none'}

CUSTOMER ORDERS:
{order_lines}

KNOWLEDGE BASE:
{kb}""")
    plan = result.model_dump()
    plan["order_number"] = (plan.get("order_number") or t["order_number"] or "").upper() or None
    db.log_event(t["id"], "resolution_planned", plan)
    return {"plan": plan}


def check_policy(state: TicketState) -> dict:
    """Hard business rules that the LLM cannot override."""
    plan = state["plan"]
    order = next((o for o in state["orders"] if o["order_number"] == plan["order_number"]), None)
    issue = None
    if not order:
        issue = f"Order {plan['order_number']} does not belong to this customer."
    elif plan["action"] == "refund":
        amount = plan.get("refund_amount") or 0
        if amount <= 0 or amount > float(order["total"]):
            issue = f"Refund Rs {amount} must be between 0 and the order total Rs {order['total']}."
    elif plan["action"] == "replacement":
        if order["status"] != "delivered":
            issue = f"Replacement needs a delivered order; {order['order_number']} is {order['status']}."
        elif (date.today() - date.fromisoformat(order["order_date"])).days > 365:
            issue = "Order is outside the 1-year warranty."
    if issue:
        db.log_event(state["ticket_id"], "policy_check_failed", {"issue": issue})
    return {"policy_issue": issue}


def human_approval(state: TicketState) -> dict:
    plan = state["plan"]
    decision = interrupt({
        "ticket_id": state["ticket_id"],
        "action": plan["action"],
        "order_number": plan["order_number"],
        "refund_amount": plan.get("refund_amount"),
        "internal_note": plan["internal_note"],
        "reply": plan["reply"],
    })
    db.log_event(state["ticket_id"], "human_decision", decision)
    return {"approval": decision}


def execute_action(state: TicketState) -> dict:
    plan, reviewer = state["plan"], state["approval"].get("reviewer", "reviewer")
    if plan["action"] == "refund":
        refund_id = db.record_refund(state["ticket_id"], plan["order_number"], plan["refund_amount"],
                                     plan["internal_note"], reviewer)
        db.log_event(state["ticket_id"], "refund_issued", {"refund_id": refund_id, "amount": plan["refund_amount"]})
    else:
        db.log_event(state["ticket_id"], "replacement_ordered", {"order_number": plan["order_number"]})
    return {}


def resolve(state: TicketState) -> dict:
    plan = state["plan"]
    if plan["action"] == "return_pickup":
        db.log_event(state["ticket_id"], "return_pickup_scheduled", {"order_number": plan["order_number"]})
    db.update_ticket(state["ticket_id"], status="resolved", resolution=plan["reply"])
    db.log_event(state["ticket_id"], "resolved", {"reply": plan["reply"]})
    return {"outcome": "resolved"}


def escalate(state: TicketState) -> dict:
    if state.get("approval") and not state["approval"].get("approved"):
        reason = f"Reviewer rejected the proposed {state['plan']['action']}: {state['approval'].get('note') or 'no note'}"
    elif state.get("policy_issue"):
        reason = f"Policy check failed: {state['policy_issue']}"
    elif state.get("plan"):
        reason = f"Agent not confident enough or chose to escalate: {state['plan']['internal_note']}"
    else:
        reason = f"Triage requires a human: {state['triage']['reason']}"
    db.update_ticket(state["ticket_id"], status="escalated", resolution=reason)
    db.log_event(state["ticket_id"], "escalated", {"reason": reason})
    return {"outcome": "escalated", "escalation_reason": reason}


# ---------- Routing ----------

def after_triage(state: TicketState) -> str:
    return "escalate" if state["triage"]["needs_human"] else "retrieve_kb"


def after_plan(state: TicketState) -> str:
    plan = state["plan"]
    if plan["action"] == "escalate" or plan["confidence"] < MIN_CONFIDENCE:
        return "escalate"
    return "check_policy" if plan["action"] in ("refund", "replacement") else "resolve"


builder = StateGraph(TicketState)
for name, fn in [("load_ticket", load_ticket), ("triage", triage), ("retrieve_kb", retrieve_kb),
                 ("plan_resolution", plan_resolution), ("check_policy", check_policy),
                 ("human_approval", human_approval), ("execute_action", execute_action),
                 ("resolve", resolve), ("escalate", escalate)]:
    builder.add_node(name, fn)

builder.add_edge(START, "load_ticket")
builder.add_edge("load_ticket", "triage")
builder.add_conditional_edges("triage", after_triage, ["escalate", "retrieve_kb"])
builder.add_edge("retrieve_kb", "plan_resolution")
builder.add_conditional_edges("plan_resolution", after_plan, ["escalate", "check_policy", "resolve"])
builder.add_conditional_edges("check_policy", lambda s: "escalate" if s["policy_issue"] else "human_approval",
                              ["escalate", "human_approval"])
builder.add_conditional_edges("human_approval",
                              lambda s: "execute_action" if s["approval"].get("approved") else "escalate",
                              ["execute_action", "escalate"])
builder.add_edge("execute_action", "resolve")
builder.add_edge("resolve", END)
builder.add_edge("escalate", END)

graph = builder.compile(checkpointer=InMemorySaver())
