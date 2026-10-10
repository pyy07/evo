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
        raise HTTPException(status_code=401, detail="未授权")


class ApproveBody(BaseModel):
    notes: str | None = None


class RejectBody(BaseModel):
    notes: str | None = None


class ImplementBody(BaseModel):
    notes: str | None = None
    capability_id: str | None = None
    name: str | None = None
    description: str = ""
    category: str = "custom"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    implementation: str = "noop"


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
                "usage_notes": d.usage_notes or [],
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


@router.get("/desk")
def desk(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Account + today's decisions/orders + trades history + settlements."""
    return {
        "portfolio": portfolio_svc.portfolio_view(db),
        "today": portfolio_svc.desk_today(db),
        "trades_history": portfolio_svc.list_trades(db, limit=200, exclude_today=True)[
            "items"
        ],
        "settlements": portfolio_svc.list_settlements(db, limit=60)["items"],
        "settlement_today": portfolio_svc.get_settlement(db),
    }


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
                "verification_notes": r.verification_notes,
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
        raise HTTPException(404, "未找到变更请求")
    if cr.status != ChangeRequestStatus.proposed:
        raise HTTPException(400, f"当前状态不可审批：{cr.status.value}")
    cr.status = ChangeRequestStatus.pending_dev
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
        raise HTTPException(404, "未找到变更请求")
    if cr.status not in (
        ChangeRequestStatus.proposed,
        ChangeRequestStatus.pending_dev,
        ChangeRequestStatus.approved,
    ):
        raise HTTPException(400, f"当前状态不可拒绝：{cr.status.value}")
    cr.status = ChangeRequestStatus.rejected
    cr.review_notes = (body.notes or "").strip() or None
    db.commit()
    return {"id": cr.id, "status": cr.status.value}


@router.post("/change-requests/{cr_id}/implement")
def implement_cr(
    cr_id: int,
    body: ImplementBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Mark CR development complete. Registering a Capability is optional."""
    cr = db.get(ChangeRequest, cr_id)
    if not cr:
        raise HTTPException(404, "未找到变更请求")
    if cr.status not in (ChangeRequestStatus.pending_dev, ChangeRequestStatus.approved):
        raise HTTPException(400, "须处于待开发状态才能标记完成")
    capability_id = (body.capability_id or "").strip() or None
    if capability_id:
        if db.get(Capability, capability_id):
            raise HTTPException(400, "capability_id 已存在")
        cap = Capability(
            id=capability_id,
            name=(body.name or "").strip() or capability_id,
            description=body.description,
            category=body.category,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            permission="agent",
            implementation=body.implementation,
            status=CapabilityStatus.active,
        )
        db.add(cap)
        cr.implemented_capability_id = capability_id
        cr.capability_payload = body.model_dump()
    cr.status = ChangeRequestStatus.completed
    cr.review_notes = body.notes or cr.review_notes
    db.commit()
    return {
        "id": cr.id,
        "status": cr.status.value,
        "capability_id": cr.implemented_capability_id,
    }


@router.get("/memory")
def memory(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    return memory_summary(db)