"""Customer support agent CLI.

    python main.py list                 list tickets
    python main.py process              work every open ticket
    python main.py process --ticket 3   work one ticket
    python main.py show 3               ticket details + audit trail
    python main.py new                  create a ticket
"""
import argparse
import json
import uuid

from langgraph.types import Command

import support_db as db
from graph import graph


def ask_approval(request: dict, reviewer: str) -> dict:
    print("\n  >>> APPROVAL NEEDED")
    print(f"  Action : {request['action']} on {request['order_number']}"
          + (f" for Rs {request['refund_amount']:,.2f}" if request.get("refund_amount") else ""))
    print(f"  Note   : {request['internal_note']}")
    print(f"  Reply  : {request['reply']}")
    answer = input("  Approve? [y/N] ").strip().lower()
    note = "" if answer in ("y", "yes") else input("  Reason for rejecting (optional): ").strip()
    return {"approved": answer in ("y", "yes"), "reviewer": reviewer, "note": note}


def process(ticket_id: int, reviewer: str) -> None:
    ticket = db.get_ticket(ticket_id)
    print(f"\n=== Ticket #{ticket_id}: {ticket['subject']} ({ticket['customers']['name']}) ===")
    config = {"configurable": {"thread_id": f"ticket-{ticket_id}-{uuid.uuid4().hex[:6]}"}}
    result = graph.invoke({"ticket_id": ticket_id}, config)
    while "__interrupt__" in result:
        decision = ask_approval(result["__interrupt__"][0].value, reviewer)
        result = graph.invoke(Command(resume=decision), config)

    t = result["triage"]
    print(f"  Triage : {t['category']} | {t['priority']} | {t['sentiment']} - {t['reason']}")
    if result.get("kb"):
        print(f"  KB     : {', '.join(a['title'] for a in result['kb'])}")
    if result.get("plan"):
        p = result["plan"]
        print(f"  Plan   : {p['action']} (confidence {p['confidence']:.2f})")
    print(f"  Outcome: {result['outcome'].upper()}")
    if result["outcome"] == "resolved":
        print(f"  Reply to customer:\n    " + result["plan"]["reply"].replace("\n", "\n    "))
    else:
        print(f"  Escalation reason: {result['escalation_reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LangGraph customer support agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    p = sub.add_parser("process")
    p.add_argument("--ticket", type=int)
    p.add_argument("--reviewer", default="support-lead")
    sub.add_parser("show").add_argument("ticket", type=int)
    sub.add_parser("new")
    args = parser.parse_args()

    if args.command == "list":
        for t in db.list_tickets():
            print(f"#{t['id']:<3} {t['status']:<10} {t['priority'] or '-':<7} {t['category'] or '-':<14} "
                  f"{t['customers']['name']:<8} {t['subject']}")
    elif args.command == "process":
        ids = [args.ticket] if args.ticket else [t["id"] for t in db.list_tickets("open")]
        if not ids:
            print("No open tickets.")
        for ticket_id in ids:
            try:
                process(ticket_id, args.reviewer)
            except Exception as e:
                print(f"  [error] ticket #{ticket_id}: {e}")
    elif args.command == "show":
        t = db.get_ticket(args.ticket)
        if not t:
            print(f"No ticket #{args.ticket}.")
            return
        print(json.dumps(t, indent=2, default=str))
        print("\nAudit trail:")
        for e in db.ticket_events(args.ticket):
            print(f"  {e['created_at'][:19]}  {e['event']:<22} {json.dumps(e['detail'])[:110]}")
    else:
        ticket_id = db.create_ticket(input("Customer email: "), input("Subject: "),
                                     input("Message: "), input("Order number (optional): "))
        print(f"Created ticket #{ticket_id}" if ticket_id else "No customer with that email.")


if __name__ == "__main__":
    main()
