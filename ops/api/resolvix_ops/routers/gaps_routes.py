"""Gap routes (owned by agent G). Register endpoints on `router`; do not rename it."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status as http_status
from pydantic import BaseModel, ConfigDict
from resolvix_contracts import GapCreate
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..audit import write_audit
from ..deps import Principal, actor_of, get_db, get_principal, system_principal, get_read_db
from ..gaps.cluster import get_strategy
from ..gaps.service import cluster_to_dict, record_gap
from ..orm import GapCluster
from ..scope import scope_department
from ..sse import GAP_CREATED
from ..timeutil import to_utc_naive

router = APIRouter(prefix="/v1", tags=["gaps"])


class ClusterPatch(BaseModel):
    """PATCH body of ops-api.yaml: {status: new | needs_document | answered | ignored}."""

    model_config = ConfigDict(extra="ignore")  # the yaml schema is an open object
    status: Literal["new", "needs_document", "answered", "ignored"]


@router.post("/gaps", status_code=201)
def create_gap(
    request: Request,
    gap_create: GapCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(system_principal),
):
    """Log one unanswered question (A's log_gap). Stored and clustered in one transaction."""
    if not gap_create.question.strip():
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="question must not be blank")

    state = request.app.state
    strategy = get_strategy(state.settings.gap_cluster_strategy, state.settings)
    now = to_utc_naive(state.clock.now())

    gap, cluster = record_gap(
        db,
        strategy,
        question=gap_create.question,
        lang=gap_create.lang,
        department_guess=gap_create.department_guess,
        call_id=gap_create.call_id,
        ticket_id=gap_create.ticket_id,
        now=now,
    )
    payload = {
        "gap_id": gap.id,
        "cluster_id": cluster.cluster_id,
        "label": cluster.label,
        "department_guess": gap.department_guess,
        "count": cluster.count,
    }
    db.commit()

    state.sse_hub.publish(GAP_CREATED, payload)
    return {"status": "ok", "gap_id": payload["gap_id"], "cluster_id": payload["cluster_id"], "count": payload["count"]}


@router.get("/gaps/clusters")
def list_clusters(
    request: Request,
    status: Optional[Literal["new", "needs_document", "answered", "ignored"]] = Query(
        None, description="Additive: only clusters with this status"),
    db: Session = Depends(get_read_db),
    principal: Principal = Depends(get_principal),
):
    """Clusters sorted by count desc, then last_seen desc (the dashboard gap table)."""
    query = db.query(GapCluster)
    scoped = scope_department(principal, request.app.state.settings)
    if scoped is not None:
        query = query.filter(GapCluster.department == scoped)
    if status is not None:
        query = query.filter(GapCluster.status == status)
    clusters = query.order_by(desc(GapCluster.count), desc(GapCluster.last_seen), GapCluster.cluster_id).all()
    return {"items": [cluster_to_dict(c) for c in clusters]}


@router.patch("/gaps/clusters/{cluster_id}")
def patch_cluster(
    request: Request,
    cluster_id: str,
    patch: ClusterPatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Set a cluster status (dashboard). Audited, including a no-op change."""
    cluster = db.get(GapCluster, cluster_id)
    scoped = scope_department(principal, request.app.state.settings)
    if cluster is not None and scoped is not None and cluster.department != scoped:
        cluster = None
    if cluster is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Gap cluster {cluster_id} not found")

    old_status = cluster.status
    cluster.status = patch.status
    write_audit(db, actor_of(principal), "gap_cluster_updated", "gap_cluster", cluster_id,
                {"status": {"from": old_status, "to": patch.status}}, commit=False)
    db.commit()
    return cluster_to_dict(cluster)
