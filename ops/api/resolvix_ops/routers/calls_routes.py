"""Call-related routes."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status as http_status
from resolvix_contracts import CallEnd, CallStart, TurnSummary
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..audit import write_audit
from ..deps import Principal, actor_of, get_db, get_principal, system_principal, get_read_db
from ..orm import Call, Turn
from ..orm import Ticket
from ..privacy import mask_number
from ..scope import scope_department
from ..timeutil import iso_utc, now_utc, to_utc_naive

router = APIRouter(prefix="/v1", tags=["calls"])


def _not_found(call_id: str) -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Call {call_id} not found")


def call_to_dict(call: Call, reveal: bool = False) -> dict:
    """CallSummary / call detail shape. Numbers are masked unless reveal."""
    return {
        "call_id": call.call_id,
        "channel": call.channel,
        "caller_number": call.caller_number if reveal else mask_number(call.caller_number),
        "started_at": iso_utc(call.started_at),
        "ended_at": iso_utc(call.ended_at),
        "kb_version": call.kb_version,
        "lang": call.lang,
        "outcome": call.outcome,
        "ai_resolved": call.ai_resolved,
        "turns_count": call.turns_count,
    }


@router.post("/calls", status_code=201)
def register_call(
    request: Request,
    response: Response,
    call_start: CallStart,
    db: Session = Depends(get_db),
    principal: Principal = Depends(system_principal),
):
    """Register a call at start (A). Idempotent on call_id: 201 first time, 200 afterwards (no event)."""
    if db.get(Call, call_start.call_id) is not None:
        response.status_code = http_status.HTTP_200_OK
        return {"status": "ok", "created": False}

    call = Call(
        call_id=call_start.call_id,
        channel=call_start.channel,
        caller_number=call_start.caller_number,
        started_at=to_utc_naive(call_start.started_at),
        kb_version=call_start.kb_version,
        lang=call_start.lang,
        turns_count=0,
    )
    db.add(call)
    db.commit()

    request.app.state.sse_hub.publish("call.started", {
        "call_id": call.call_id,
        "channel": call.channel,
        "caller_number": mask_number(call.caller_number),
        "started_at": iso_utc(call.started_at),
        "kb_version": call.kb_version,
        "lang": call.lang,
    })
    return {"status": "ok", "created": True}


@router.get("/calls")
def list_calls(
    request: Request,
    from_: Optional[datetime] = Query(None, alias="from"),
    outcome: Optional[str] = None,
    active: bool = Query(False, description="Additive: only calls that have not ended"),
    limit: int = Query(50, ge=1, le=1000),
    reveal: bool = Query(False, description="Not supported on lists: use GET /v1/calls/{id}?reveal=true"),
    db: Session = Depends(get_read_db),
    principal: Principal = Depends(get_principal),
):
    """List calls, newest first (dashboard). Numbers are always masked here."""
    if reveal:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="reveal is only available on GET /v1/calls/{call_id}")
    query = db.query(Call)
    scoped = scope_department(principal, request.app.state.settings)
    if scoped is not None:  # staff: only calls that produced a ticket in their department
        query = query.filter(Call.call_id.in_(
            db.query(Ticket.call_id).filter(Ticket.department == scoped, Ticket.call_id.is_not(None))))
    if from_ is not None:
        query = query.filter(Call.started_at >= to_utc_naive(from_))
    if outcome:
        query = query.filter(Call.outcome == outcome)
    if active:
        query = query.filter(Call.ended_at.is_(None))
    calls = query.order_by(desc(Call.started_at), desc(Call.call_id)).limit(limit).all()

    items = []
    latest: dict[str, Turn] = {}  # one query for the latest turn of every listed call
    if calls:
        for t in db.query(Turn).filter(Turn.call_id.in_([c.call_id for c in calls])).order_by(Turn.turn_index):
            latest[t.call_id] = t
    for c in calls:
        item = call_to_dict(c)
        # additive: lets the live board show the latest path/tier after a page reload
        last = latest.get(c.call_id)
        item["last_turn"] = None if last is None else {
            "turn_index": last.turn_index,
            "path": last.summary_json.get("path"),
            "tier": last.summary_json.get("tier"),
            "lang": last.summary_json.get("lang"),
        }
        items.append(item)
    return {"items": items}


@router.post("/calls/{call_id}/turns", status_code=201)
def append_turn(
    call_id: str,
    request: Request,
    response: Response,
    turn_summary: TurnSummary,
    db: Session = Depends(get_db),
    principal: Principal = Depends(system_principal),
):
    """Append one TurnSummary (A). Idempotent on (call_id, turn_index): first write wins, duplicate -> 200, no event."""
    if turn_summary.call_id != call_id:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"call_id in body ({turn_summary.call_id}) does not match path ({call_id})",
        )
    call = db.get(Call, call_id)
    if call is None:
        raise _not_found(call_id)

    existing = db.query(Turn).filter(
        Turn.call_id == call_id, Turn.turn_index == turn_summary.turn_index
    ).first()
    if existing is not None:
        response.status_code = http_status.HTTP_200_OK
        return existing.summary_json

    stored = turn_summary.model_dump(mode="json")
    db.add(Turn(
        call_id=call_id,
        turn_index=turn_summary.turn_index,
        summary_json=stored,
        created_at=now_utc(),
    ))
    call.turns_count = max(call.turns_count or 0, turn_summary.turn_index + 1)
    db.commit()

    request.app.state.sse_hub.publish("call.turn", {
        "call_id": call_id,
        "turn_index": turn_summary.turn_index,
        "path": turn_summary.path,
        "tier": turn_summary.tier,
        "lang": turn_summary.lang,
        "priority": turn_summary.priority,
        "turns_count": call.turns_count,
    })
    return stored


@router.post("/calls/{call_id}/end")
def end_call(
    call_id: str,
    request: Request,
    call_end: CallEnd,
    db: Session = Depends(get_db),
    principal: Principal = Depends(system_principal),
):
    """End a call (A). The first end wins; later ends change nothing and emit no event."""
    call = db.get(Call, call_id)
    if call is None:
        raise _not_found(call_id)

    if call.ended_at is not None:
        return {"status": "ok", "updated": False}

    call.ended_at = to_utc_naive(call_end.ended_at)
    call.outcome = call_end.outcome
    call.ai_resolved = call_end.ai_resolved
    call.turns_count = call_end.turns_count
    db.commit()

    request.app.state.sse_hub.publish("call.ended", {
        "call_id": call_id,
        "ended_at": iso_utc(call.ended_at),
        "outcome": call.outcome,
        "ai_resolved": call.ai_resolved,
        "turns_count": call.turns_count,
    })
    return {"status": "ok", "updated": True}


@router.get("/calls/{call_id}")
def get_call_detail(
    call_id: str,
    request: Request,
    reveal: bool = Query(False, description="Additive: return the full caller number (audited)"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Call detail with turns (dashboard). Always writes a transcript_viewed audit entry."""
    call = db.get(Call, call_id)
    scoped = scope_department(principal, request.app.state.settings)
    if call is not None and scoped is not None and db.query(Ticket.id).filter(
            Ticket.call_id == call_id, Ticket.department == scoped).first() is None:
        call = None
    if call is None:
        raise _not_found(call_id)

    turns = db.query(Turn).filter(Turn.call_id == call_id).order_by(Turn.turn_index).all()

    actor = actor_of(principal)
    write_audit(db, actor, "transcript_viewed", "call", call_id, commit=False)
    if reveal and call.caller_number:
        write_audit(db, actor, "number_revealed", "call", call_id, {"field": "caller_number"}, commit=False)
    db.commit()

    return {"call": call_to_dict(call, reveal), "turns": [t.summary_json for t in turns]}
