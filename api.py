"""FastAPI service for the customer support agent.

    uvicorn api:app --port 8005      then open http://127.0.0.1:8005/docs

Processing a ticket that needs a refund or replacement pauses the graph and returns
status "needs_approval" with a thread_id; POST /approvals/{thread_id} resumes it.
"""
import os
import uuid
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from langgraph.types import Command
from ollama import ResponseError
from pydantic import BaseModel, Field

import support_db as db
from graph import graph

API_KEY = os.getenv("API_KEY")  # when set, every request needs header X-API-Key
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: Optional[str] = Security(api_key_header)):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")


app = FastAPI(title="Customer Support Agent",
              description="Triage, resolve or escalate support tickets, with human approval "
                          "for refunds and replacements.",
              dependencies=[Depends(require_api_key)])


@app.exception_handler(ResponseError)
def llm_unavailable(request, exc: ResponseError):
    return JSONResponse(status_code=503, content={"detail": f"LLM unavailable: {exc.error}"})


class NewTicket(BaseModel):
    email: str
    subject: str
    body: str
    order_number: Optional[str] = None


class Approval(BaseModel):
    approved: bool
    reviewer: str = "api-reviewer"
    note: str = ""


class ProcessResult(BaseModel):
    ticket_id: int
    thread_id: str
    status: Literal["resolved", "escalated", "needs_approval"]
    triage: Optional[dict] = None
    kb_articles: list[str] = []
    plan: Optional[dict] = None
    approval_request: Optional[dict] = Field(None, description="Proposed action waiting for a human")
    reply: Optional[str] = Field(None, description="Message sent to the customer when resolved")
    escalation_reason: Optional[str] = None


def to_result(thread_id: str, state: dict) -> ProcessResult:
    common = dict(ticket_id=state["ticket_id"], thread_id=thread_id, triage=state.get("triage"),
                  kb_articles=[a["title"] for a in state.get("kb") or []], plan=state.get("plan"))
    if state.get("__interrupt__"):
        return ProcessResult(**common, status="needs_approval",
                             approval_request=state["__interrupt__"][0].value)
    if state["outcome"] == "resolved":
        return ProcessResult(**common, status="resolved", reply=state["plan"]["reply"])
    return ProcessResult(**common, status="escalated", escalation_reason=state["escalation_reason"])


@app.get("/tickets", operation_id="listTickets")
def list_tickets(status: Optional[Literal["open", "resolved", "escalated", "closed"]] = None):
    return db.list_tickets(status)


@app.get("/tickets/{ticket_id}", operation_id="getTicket")
def get_ticket(ticket_id: int):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return {**ticket, "events": db.ticket_events(ticket_id)}


@app.post("/tickets", status_code=201, operation_id="createTicket")
def create_ticket(data: NewTicket):
    ticket_id = db.create_ticket(data.email, data.subject, data.body, data.order_number)
    if ticket_id is None:
        raise HTTPException(status_code=404, detail="No customer with that email.")
    return {"ticket_id": ticket_id}


@app.post("/tickets/{ticket_id}/process", response_model=ProcessResult, operation_id="processTicket")
def process_ticket(ticket_id: int):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    if ticket["status"] != "open":
        raise HTTPException(status_code=409, detail=f"Ticket is already {ticket['status']}.")
    thread_id = f"ticket-{ticket_id}-{uuid.uuid4().hex[:6]}"
    state = graph.invoke({"ticket_id": ticket_id}, {"configurable": {"thread_id": thread_id}})
    return to_result(thread_id, state)


@app.post("/approvals/{thread_id}", response_model=ProcessResult, operation_id="decideApproval")
def decide(thread_id: str, data: Approval):
    config = {"configurable": {"thread_id": thread_id}}
    if not graph.get_state(config).interrupts:
        raise HTTPException(status_code=404, detail="No approval pending for this thread_id.")
    state = graph.invoke(Command(resume=data.model_dump()), config)
    return to_result(thread_id, state)


@app.get("/health")
def health():
    return {"status": "ok"}
