"""Ticket-related routes."""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status as http_status
from resolvix_contracts import TicketCreate, TicketCreated, TicketEvent as TicketEventContract, TicketPatch
from sqlalchemy import case, desc
from sqlalchemy.orm import Session

from ..audit import write_audit
from ..deps import Principal, actor_of, get_db, get_principal, system_principal, get_read_db
from ..orm import Ticket, TicketEvent, TicketSeq
from ..privacy import mask_number
from ..scope import scope_department
from ..timeutil import iso_utc, now_utc, to_utc_naive

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["tickets"])

# Any status transition is allowed. These decide how resolved_at follows the status.
_DONE_STATUSES = {"resolved", "closed"}
_REOPEN_STATUSES = {"open", "in_progress"}


def _scoped_ticket(db, request, principal, ticket_id: str):
    """The ticket, or 404 when it does not exist or is outside the department of the principal."""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    dept = scope_department(principal, request.app.state.settings)
    if ticket is None or (dept is not None and ticket.department != dept):
        raise _not_found(ticket_id)
    return ticket


def _not_found(ticket_id: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Ticket {ticket_id} not found")


def allocate_ticket_id(db: Session, department_prefix: str) -> str:
    """Take the next global sequence number.

    Runs inside the caller's transaction, which SQLite opened with BEGIN IMMEDIATE (see db.py),
    so concurrent creators are serialised and a rolled-back insert gives the number back.
    """
    seq = db.get(TicketSeq, 1)
    if seq is None:
        seq = TicketSeq(id=1, next_num=1001)
        db.add(seq)
        db.flush()
    ticket_num = seq.next_num
    if ticket_num > 9999:  # the contract id pattern is ^[A-Z]{2}-\d{4}$
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="ticket id space exhausted (maximum 9999)")
    seq.next_num = ticket_num + 1
    db.flush()
    return f"{department_prefix}-{ticket_num:04d}"


def ticket_to_dict(ticket: Ticket, reveal: bool = False) -> dict:
    """Ticket contract shape. Numbers are masked unless reveal."""
    def num(value: str | None) -> str | None:
        return value if reveal else mask_number(value)

    return {
        "ticket_id": ticket.ticket_id,
        "call_id": ticket.call_id,
        "type": ticket.type,
        "priority": ticket.priority,
        "priority_reason": ticket.priority_reason,
        "department": ticket.department,
        "status": ticket.status,
        "summary": ticket.summary,
        "collected_fields": ticket.collected_fields or {},
        "caller_number": num(ticket.caller_number),
        "callback_number": num(ticket.callback_number),
        "callback_requested": bool(ticket.callback_requested),
        "transcript_excerpt": ticket.transcript_excerpt or "",
        "brief": ticket.brief or "",
        "assignee": ticket.assignee,
        "notes": list(ticket.notes or []),
        "created_at": iso_utc(ticket.created_at),
        "updated_at": iso_utc(ticket.updated_at),
        "resolved_at": iso_utc(ticket.resolved_at),
    }


_NUMBER_IN_TEXT = re.compile(r"\+?\d{8,15}")


def mask_payload(payload: dict) -> dict:
    """Event payloads written by A can carry phone numbers (transfer targets, callers): never return them in full."""
    return {k: _NUMBER_IN_TEXT.sub(lambda m: mask_number(m.group(0)) or "", v) if isinstance(v, str) else v
            for k, v in payload.items()}


def _audit_reveal(db: Session, actor: str, tickets: list[Ticket]) -> None:
    for t in tickets:
        if t.caller_number or t.callback_number:
            write_audit(db, actor, "number_revealed", "ticket", t.ticket_id,
                        {"fields": "caller_number,callback_number"}, commit=False)


@router.post("/tickets", status_code=201)
def create_ticket(
    request: Request,
    response: Response,
    ticket_create: TicketCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(system_principal),
):
    """Create ticket (A). Idempotent on idempotency_key: 201 created=true, 200 created=false (no event).

    The idempotency lookup and the department check both run before the sequence is touched,
    so duplicates and rejected requests never consume a number.
    """
    existing = db.query(Ticket).filter(Ticket.idempotency_key == ticket_create.idempotency_key).first()
    if existing is not None:
        response.status_code = http_status.HTTP_200_OK
        return TicketCreated(ticket_id=existing.ticket_id, created=False).model_dump(mode="json")

    dept_map = {d.key: d for d in request.app.state.departments_config.departments}
    dept = dept_map.get(ticket_create.department)
    if dept is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown department: {ticket_create.department}",
        )

    ticket_id = allocate_ticket_id(db, dept.ticket_prefix)
    now = now_utc()
    ticket = Ticket(
        ticket_id=ticket_id,
        idempotency_key=ticket_create.idempotency_key,
        call_id=ticket_create.call_id,
        type=ticket_create.type,
        priority=ticket_create.priority,
        priority_reason=ticket_create.priority_reason,
        department=ticket_create.department,
        status="open",
        summary=ticket_create.summary,
        collected_fields=ticket_create.collected_fields,
        caller_number=ticket_create.caller_number,
        callback_number=ticket_create.callback_number,
        callback_requested=ticket_create.callback_requested,
        transcript_excerpt=ticket_create.transcript_excerpt,
        brief=ticket_create.brief,
        notes=[],
        created_at=now,
        updated_at=now,
    )
    db.add(ticket)
    db.commit()

    hub = request.app.state.sse_hub
    hub.publish("ticket.created", {
        "ticket_id": ticket_id,
        "call_id": ticket.call_id,
        "type": ticket.type,
        "priority": ticket.priority,
        "priority_reason": ticket.priority_reason,
        "department": ticket.department,
        "status": ticket.status,
        "summary": ticket.summary,
        "caller_number": mask_number(ticket.caller_number),
        "callback_number": mask_number(ticket.callback_number),
        "callback_requested": ticket.callback_requested,
        "created_at": iso_utc(ticket.created_at),
    })
    if ticket.priority == "P1":
        hub.publish("alert.p1", {
            "ticket_id": ticket_id,
            "call_id": ticket.call_id,
            "type": ticket.type,
            "department": ticket.department,
            "priority": ticket.priority,
            "priority_reason": ticket.priority_reason,
            "summary": ticket.summary,
            "caller_number": mask_number(ticket.caller_number),
            "callback_number": mask_number(ticket.callback_number),
            "created_at": iso_utc(ticket.created_at),
        })

    return TicketCreated(ticket_id=ticket_id, created=True).model_dump(mode="json")


@router.get("/tickets")
def list_tickets(
    request: Request,
    status: Optional[str] = None,
    department: Optional[str] = None,
    priority: Optional[str] = None,
    caller_number: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    reveal: bool = Query(False, description="Not supported on lists: use GET /v1/tickets/{id}?reveal=true"),
    db: Session = Depends(get_read_db),
    principal: Principal = Depends(get_principal),
):
    """List tickets: P1 first, then newest first (created_at desc, then sequence desc). Numbers are always masked."""
    if reveal:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="reveal is only available on GET /v1/tickets/{ticket_id}")
    query = db.query(Ticket)
    scoped = scope_department(principal, request.app.state.settings)
    if scoped is not None:
        query = query.filter(Ticket.department == scoped)
    if status:
        query = query.filter(Ticket.status == status)
    if department:
        query = query.filter(Ticket.department == department)
    if priority:
        query = query.filter(Ticket.priority == priority)
    if caller_number:
        query = query.filter(Ticket.caller_number == caller_number)

    priority_rank = case(
        (Ticket.priority == "P1", 1),
        (Ticket.priority == "P2", 2),
        (Ticket.priority == "P3", 3),
        (Ticket.priority == "P4", 4),
        else_=5,
    )
    tickets = query.order_by(priority_rank, desc(Ticket.created_at), desc(Ticket.id)).limit(limit).all()

    return {"items": [ticket_to_dict(t) for t in tickets]}


@router.get("/tickets/{ticket_id}")
def get_ticket_detail(
    ticket_id: str,
    request: Request,
    reveal: bool = Query(False, description="Additive: return full numbers (audited)"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    ticket = _scoped_ticket(db, request, principal, ticket_id)
    if reveal:
        _audit_reveal(db, actor_of(principal), [ticket])
        db.commit()
    return ticket_to_dict(ticket, reveal)


@router.patch("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: str,
    request: Request,
    patch: TicketPatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Staff update (dashboard). Notes are appended (never replaced); every PATCH is audited.

    A real status change writes a status_changed event and calls notifier.on_ticket_status_changed
    exactly once, after commit. Setting the same status again is not a change.
    """
    ticket = _scoped_ticket(db, request, principal, ticket_id)

    actor = actor_of(principal)
    now = now_utc()
    old_status = ticket.status
    status_changed = patch.status is not None and patch.status != old_status
    audit: dict[str, str] = {}

    if status_changed:
        ticket.status = patch.status
        if patch.status in _DONE_STATUSES and ticket.resolved_at is None:
            ticket.resolved_at = now
        elif patch.status in _REOPEN_STATUSES:
            ticket.resolved_at = None
        audit["status"] = f"{old_status} -> {patch.status}"
        db.add(TicketEvent(
            ticket_id=ticket_id, kind="status_changed", actor=actor, at=now,
            payload={"old_status": old_status, "new_status": patch.status},
        ))

    if patch.assignee is not None:
        ticket.assignee = patch.assignee
        audit["assignee"] = patch.assignee

    note_texts: list[str] = []
    if patch.note:
        note_texts.append(patch.note)
        audit["note_added"] = "yes"
    if patch.resolution:
        # Contract quirk: TicketPatch.resolution has no matching Ticket field; kept as a labelled note.
        note_texts.append(f"Resolution: {patch.resolution}")
        audit["resolution_added"] = "yes"
    if note_texts:
        stamp = iso_utc(now)
        ticket.notes = list(ticket.notes or []) + [f"[{stamp}] {actor}: {text}" for text in note_texts]
        for text in note_texts:
            db.add(TicketEvent(ticket_id=ticket_id, kind="note_added", actor=actor, at=now,
                               payload={"note": text}))

    ticket.updated_at = now
    write_audit(db, actor, "ticket_updated", "ticket", ticket_id, audit, commit=False)
    db.commit()

    if status_changed:
        try:
            request.app.state.notifier.on_ticket_status_changed(db, ticket, old_status, ticket.status, actor)
        except Exception:  # the change is already committed; SMS trouble must not fail the PATCH
            log.exception("notifier failed for ticket %s", ticket_id)

    request.app.state.sse_hub.publish("ticket.updated", {
        "ticket_id": ticket_id,
        "status": ticket.status,
        "assignee": ticket.assignee,
        "priority": ticket.priority,
        "department": ticket.department,
        "updated_at": iso_utc(ticket.updated_at),
    })
    return ticket_to_dict(ticket)


@router.post("/tickets/{ticket_id}/events", status_code=201)
def add_ticket_event(
    ticket_id: str,
    request: Request,
    event: TicketEventContract,
    db: Session = Depends(get_db),
    principal: Principal = Depends(system_principal),
):
    """Append a ticket event (A reports transfers; C internal for SMS)."""
    _scoped_ticket(db, request, principal, ticket_id)
    db.add(TicketEvent(
        ticket_id=ticket_id, kind=event.kind, payload=event.payload,
        actor=event.actor, at=to_utc_naive(event.at),
    ))
    db.commit()
    return {"status": "ok"}


@router.get("/tickets/{ticket_id}/events")
def list_ticket_events(
    ticket_id: str,
    request: Request,
    db: Session = Depends(get_read_db),
    principal: Principal = Depends(get_principal),
):
    """Ticket events, oldest first."""
    _scoped_ticket(db, request, principal, ticket_id)
    events = (
        db.query(TicketEvent).filter(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.at, TicketEvent.id).all()
    )
    return {"items": [
        {"kind": e.kind, "payload": mask_payload(e.payload or {}), "actor": e.actor, "at": iso_utc(e.at)}
        for e in events
    ]}
