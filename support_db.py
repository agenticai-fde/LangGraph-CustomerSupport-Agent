"""Supabase data access for the support agent."""
from datetime import datetime, timezone
from typing import Optional

from config import get_db


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_ticket(ticket_id: int) -> Optional[dict]:
    rows = (get_db().table("support_tickets").select("*, customers(id,name,email,plan,city)")
            .eq("id", ticket_id).execute().data)
    return rows[0] if rows else None


def get_orders(customer_id: int) -> list[dict]:
    return (get_db().table("orders")
            .select("order_number,status,order_date,quantity,total,products(name,category)")
            .eq("customer_id", customer_id).order("order_date", desc=True).execute().data)


def search_kb(text: str, k: int = 3) -> list[dict]:
    return get_db().rpc("search_kb_articles", {"q": text, "k": k}).execute().data


def update_ticket(ticket_id: int, **fields) -> None:
    get_db().table("support_tickets").update({**fields, "updated_at": now()}).eq("id", ticket_id).execute()


def log_event(ticket_id: int, event: str, detail: Optional[dict] = None) -> None:
    get_db().table("ticket_events").insert(
        {"ticket_id": ticket_id, "event": event, "detail": detail or {}}).execute()


def record_refund(ticket_id: int, order_number: str, amount: float, reason: str, approved_by: str) -> int:
    row = get_db().table("refunds").insert({
        "ticket_id": ticket_id, "order_number": order_number, "amount": amount,
        "reason": reason, "approved_by": approved_by,
    }).execute().data[0]
    return row["id"]


def list_tickets(status: Optional[str] = None) -> list[dict]:
    query = (get_db().table("support_tickets")
             .select("id,subject,status,category,priority,order_number,customers(name)").order("id"))
    if status:
        query = query.eq("status", status)
    return query.execute().data


def ticket_events(ticket_id: int) -> list[dict]:
    return (get_db().table("ticket_events").select("event,detail,created_at")
            .eq("ticket_id", ticket_id).order("id").execute().data)


def create_ticket(email: str, subject: str, body: str, order_number: Optional[str]) -> Optional[int]:
    customers = get_db().table("customers").select("id").eq("email", email.strip().lower()).execute().data
    if not customers:
        return None
    row = get_db().table("support_tickets").insert({
        "customer_id": customers[0]["id"], "subject": subject, "body": body,
        "order_number": (order_number or "").strip().upper() or None, "channel": "web",
    }).execute().data[0]
    return row["id"]
