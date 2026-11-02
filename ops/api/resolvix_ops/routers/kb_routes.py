"""Knowledge-base routes (owned by agent K). Register endpoints on `router`; do not rename it."""
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from resolvix_contracts import ErrorBody, ErrorEnvelope, KBEntry
from sqlalchemy.orm import Session

from ..audit import write_audit
from ..deps import Principal, actor_of, get_db, get_principal, get_read_db
from ..kb.export import build_export
from ..kb.importer import ImportRejected, parse_jsonl
from ..kb.publish import PublishError, live_publish, mock_publish
from ..kb.store import entry_dict, entry_meta, save_draft
from ..orm import KBEntryDB, KBPublish
from ..timeutil import iso_utc, now_utc, to_utc_naive

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["kb"])


def admin_only(principal: Principal = Depends(get_principal)) -> Principal:
    """A signed-in admin session. The service key and staff sessions get 403 (error code `unauthorized`)."""
    if principal.kind != "user" or principal.role != "admin":
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return principal


def user_writer(principal: Principal = Depends(get_principal)) -> Principal:
    """A signed-in staff or admin session. The service key (the voice runtime) never writes the KB."""
    if principal.kind != "user":
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="A staff or admin session is required")
    return principal


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["draft", "approved", "archived"]


def _stored(row: KBEntryDB) -> dict:
    """Response for a stored entry: strict KBEntry under `entry`, review metadata alongside."""
    return {"id": row.id, "status": row.status, "flagged": bool(row.flagged), "entry": entry_dict(row)}


# --------------------------------------------------------------------------- entries
@router.get("/kb/entries")
def list_entries(
    status: Optional[Literal["draft", "approved", "archived"]] = None,
    db: Session = Depends(get_read_db),
    principal: Principal = Depends(get_principal),
):
    """{items: KBEntry[]} ordered by id. Every item validates as a KBEntry. The review flag is additive and
    sits outside the items: `flagged_ids` (ids whose injection scan hit) and
    `meta` ({id: {flagged, updated_at, updated_by}})."""
    q = db.query(KBEntryDB)
    if status:
        q = q.filter(KBEntryDB.status == status)
    rows = q.order_by(KBEntryDB.id).all()
    return {
        "items": [entry_dict(r) for r in rows],
        "flagged_ids": [r.id for r in rows if r.flagged],
        "meta": {r.id: entry_meta(r) for r in rows},
    }


@router.post("/kb/entries", status_code=201)
def save_entry(
    entry: KBEntry,
    db: Session = Depends(get_db),
    principal: Principal = Depends(user_writer),
):
    """Create or update one entry. It is always stored as a draft (whatever status the body carries) and
    scanned for prompt injection. Body {id, status, flagged, entry}."""
    actor = actor_of(principal)
    existing = db.get(KBEntryDB, entry.id)
    if existing is not None and existing.status in {"approved", "archived"} and principal.role != "admin":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"KB entry {entry.id} is {existing.status}; only an admin can edit it")
    row, flagged, created = save_draft(db, entry, actor)
    write_audit(db, actor, "kb_entry_saved", "kb_entry", row.id,
                {"created": created, "flagged": flagged, "department": row.department}, commit=False)
    db.commit()
    return _stored(row)


@router.post("/kb/entries/{id}/status")
def set_entry_status(
    id: str,  # noqa: A002  (named `id` to match the path in ops-api.yaml)
    body: StatusBody,
    db: Session = Depends(get_db),
    principal: Principal = Depends(admin_only),
):
    """Admin only. Approve, archive or send back to draft. A flagged entry cannot be approved (409)."""
    row = db.get(KBEntryDB, id)
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"KB entry {id} not found")
    if body.status == "approved" and row.flagged:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"KB entry {id} was flagged by the injection scan; "
                   "edit its text and save it again before approving")
    old = row.status
    actor = actor_of(principal)
    if old != body.status:
        row.status = body.status
        row.body_json = {**row.body_json, "status": body.status}
        row.updated_at = now_utc()
        row.updated_by = actor
    write_audit(db, actor, "kb_entry_status_changed", "kb_entry", row.id,
                {"old_status": old, "new_status": body.status}, commit=False)
    db.commit()
    return _stored(row)


# --------------------------------------------------------------------------- import
@router.post("/kb/import")
def import_entries(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(admin_only),
):
    """Admin only. Bulk import of kb.jsonl (multipart field `file`). Entries keep their status field (contract),
    except that an entry the injection scan flags is always saved as a draft. A repeated id inside one file is
    reported as an error line. Returns {imported, flagged, errors: [{line, message}]}."""
    settings = request.app.state.settings
    raw = file.file.read(settings.kb_import_max_bytes + 1)
    try:
        parsed = parse_jsonl(raw, settings.kb_import_max_bytes, settings.kb_import_max_lines)
    except ImportRejected as exc:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    actor = actor_of(principal)
    flagged_ids: list[str] = []
    errors = list(parsed.errors)
    seen: set[str] = set()
    imported = 0
    for line_no, entry in parsed.entries:
        if entry.id in seen:
            errors.append({"line": line_no, "message": f"duplicate id {entry.id} in this file; the first line was kept"})
            continue
        seen.add(entry.id)
        row, flagged, _created = save_draft(db, entry, actor, keep_status=True)
        db.flush()
        imported += 1
        if flagged:
            flagged_ids.append(row.id)
    write_audit(db, actor, "kb_imported", "kb_entry", "import",
                {"filename": file.filename or "", "imported": imported, "flagged": len(flagged_ids),
                 "errors": len(errors), "flagged_ids": flagged_ids[:50]}, commit=False)
    db.commit()
    errors.sort(key=lambda e: e["line"])
    return {"imported": imported, "flagged": len(flagged_ids), "errors": errors}


# --------------------------------------------------------------------------- export / publish
@router.get("/kb/export")
def export_kb(
    request: Request,
    db: Session = Depends(get_read_db),
    principal: Principal = Depends(get_principal),
):
    """KBExport: approved entries valid today (Asia/Kolkata), consumed by B's compiler."""
    return build_export(db, request.app.state.clock).model_dump(mode="json")


def _envelope(exc: PublishError) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=exc.code, message=exc.message, retryable=exc.retryable,  # type: ignore[arg-type]
                                         details=exc.details))
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))


@router.post("/kb/publish")
def publish_kb(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(admin_only),
):
    """Admin session only. Live: ask the voice runtime to reload from our export URL and return its KBVersionInfo
    (its 422 comes back as 422 with the problems, unreachable/5xx as 502 upstream_error). Mock: fixture answer."""
    settings, clock = request.app.state.settings, request.app.state.clock
    actor = actor_of(principal)
    mode = "live" if settings.runtime_live else "mock"
    try:
        if mode == "live":
            # The runtime calls GET /v1/kb/export back while we wait. No database transaction may be open across this
            # call (C-1): end whatever this session started, then talk to the runtime, then write in a new short one.
            db.rollback()
            info = live_publish(settings)
        else:
            info = mock_publish(db, clock)
    except PublishError as exc:
        write_audit(db, actor, "kb_publish_failed", "kb_publish", "-",
                    {"mode": mode, "status_code": exc.status_code, "code": exc.code, "message": exc.message,
                     "details": exc.details}, commit=True)
        return _envelope(exc)

    data = info.model_dump(mode="json")
    db.add(KBPublish(kb_version=info.kb_version, info_json=data, mode=mode, actor=actor,
                     at=to_utc_naive(clock.now())))
    write_audit(db, actor, "kb_published", "kb_publish", info.kb_version,
                {"mode": mode, "entry_count": info.entry_count, "faq_count": info.faq_count}, commit=False)
    db.commit()
    return data


@router.get("/kb/publishes")
def list_publishes(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_read_db),
    principal: Principal = Depends(get_principal),
):
    """Additive (not in ops-api.yaml): publish history, newest first."""
    rows = db.query(KBPublish).order_by(KBPublish.at.desc(), KBPublish.id.desc()).limit(limit).all()
    return {"items": [{"kb_version": r.kb_version, "info": r.info_json, "mode": r.mode, "actor": r.actor,
                       "at": iso_utc(r.at)} for r in rows]}
