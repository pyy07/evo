from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from evo_api.config import get_settings
from evo_api.db.session import get_db
from evo_api.models.entities import (
    AgentRun,
    Capability,
    CapabilityStatus,
    ChangeRequest,
    ChangeRequestStatus,
    InvestmentDecision,
    Review,
)
from evo_api.services import portfolio as portfolio_svc
from evo_api.services.memory import memory_summary

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    token = settings.admin_token
    if not authorization or authorization.removeprefix("Bearer ").strip() != token:
        raise HTTPException(status_code=401, detail="unauthorized")


class ApproveBody(BaseModel):
    notes: str | None = None


class RejectBody(BaseModel):
    notes: str | None = None


class ImplementBody(BaseModel):
    capability_id: str
    name: str
    description: str = ""
    category: str = "custom"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    implementation: str = "noop"
    notes: str | None = None


@router.get("/timeline")
def timeline(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    runs = db.query(AgentRun).order_by(AgentRun.id.desc()).limit(50).all()
    decisions = (
        db.query(InvestmentDecision).order_by(InvestmentDecision.id.desc()).limit(50).all()
    )
    reviews = db.query(Review).order_by(Review.id.desc()).limit(50).all()
    return {
        "agent_runs": [
            {
                "id": r.id,
                "status": r.status,
                "trigger": r.trigger,
                "notes": r.notes,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in runs
        ],
        "decisions": [
            {
                "id": d.id,
                "summary": d.summary,
                "hypothesis": d.hypothesis,
                "action_plan": d.action_plan,
                "agent_run_id": d.agent_run_id,
                "observation_id": d.observation_id,
                "thesis_id": d.thesis_id,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decisions
        ],
        "reviews": [
            {
                "id": r.id,
                "issue_type": r.issue_type.value,
                "content": r.content,
                "decision_id": r.decision_id,
                "change_request_id": r.change_request_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reviews
        ],
    }


@router.get("/portfolio")
def portfolio(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    return portfolio_svc.portfolio_view(db)


@router.get("/change-requests")
def list_crs(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    rows = db.query(ChangeRequest).order_by(ChangeRequest.id.desc()).all()
    return {
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "problem": r.problem,
                "evidence": r.evidence,
                "proposal": r.proposal,
                "expected_benefit": r.expected_benefit,
                "issue_type": r.issue_type.value,
                "status": r.status.value,
                "review_notes": r.review_notes,
                "implemented_capability_id": r.implemented_capability_id,
            }
            for r in rows
        ]
    }


@router.post("/change-requests/{cr_id}/approve")
def approve_cr(
    cr_id: int,
    body: ApproveBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(404, "not found")
    if cr.status != ChangeRequestStatus.proposed:
        raise HTTPException(400, f"cannot approve from status {cr.status.value}")
    cr.status = ChangeRequestStatus.approved
    cr.review_notes = body.notes
    db.commit()
    return {"id": cr.id, "status": cr.status.value}


@router.post("/change-requests/{cr_id}/reject")
def reject_cr(
    cr_id: int,
    body: RejectBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(404, "not found")
    cr.status = ChangeRequestStatus.rejected
    cr.review_notes = body.notes
    db.commit()
    return {"id": cr.id, "status": cr.status.value}


@router.post("/change-requests/{cr_id}/implement")
def implement_cr(
    cr_id: int,
    body: ImplementBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Human marks CR implemented and registers a new Capability (code still manual)."""
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(404, "not found")
    if cr.status != ChangeRequestStatus.approved:
        raise HTTPException(400, "CR must be approved before implement")
    if db.get(Capability, body.capability_id):
        raise HTTPException(400, "capability_id already exists")
    cap = Capability(
        id=body.capability_id,
        name=body.name,
        description=body.description,
        category=body.category,
        input_schema=body.input_schema,
        output_schema=body.output_schema,
        permission="agent",
        implementation=body.implementation,
        status=CapabilityStatus.active,
    )
    db.add(cap)
    cr.status = ChangeRequestStatus.implemented
    cr.implemented_capability_id = body.capability_id
    cr.capability_payload = body.model_dump()
    cr.review_notes = body.notes or cr.review_notes
    db.commit()
    return {
        "id": cr.id,
        "status": cr.status.value,
        "capability_id": body.capability_id,
    }


@router.get("/memory")
def memory(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    return memory_summary(db)