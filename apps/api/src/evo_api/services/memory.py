from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from evo_api.models.entities import (
    AuditLog,
    ChangeRequest,
    Experience,
    InvestmentDecision,
    IssueType,
    Review,
)


def memory_summary(db: Session) -> dict[str, Any]:
    decisions = db.query(func.count(InvestmentDecision.id)).scalar() or 0
    reviews = db.query(Review).all()
    by_issue: dict[str, int] = {}
    for r in reviews:
        by_issue[r.issue_type.value] = by_issue.get(r.issue_type.value, 0) + 1

    inv_failures = by_issue.get(IssueType.DecisionError.value, 0)
    tool_errors = by_issue.get(IssueType.ToolUsageError.value, 0)
    gaps = by_issue.get(IssueType.CapabilityGap.value, 0) + by_issue.get(
        IssueType.DataGap.value, 0
    )

    audit_total = db.query(func.count(AuditLog.id)).scalar() or 0
    audit_fail = db.query(func.count(AuditLog.id)).filter(AuditLog.success.is_(False)).scalar() or 0

    # top capabilities by invoke count
    rows = (
        db.query(AuditLog.capability_id, func.count(AuditLog.id))
        .group_by(AuditLog.capability_id)
        .order_by(func.count(AuditLog.id).desc())
        .limit(10)
        .all()
    )

    crs = db.query(ChangeRequest).all()
    cr_by_status: dict[str, int] = {}
    for cr in crs:
        cr_by_status[cr.status.value] = cr_by_status.get(cr.status.value, 0) + 1

    experiences = (
        db.query(Experience).order_by(Experience.id.desc()).limit(20).all()
    )
    return {
        "investment_memory": {
            "decision_count": decisions,
            "review_count": len(reviews),
            "decision_errors": inv_failures,
            "issue_breakdown": by_issue,
            "recent_experiences": [
                {"id": e.id, "kind": e.kind.value, "content": e.content} for e in experiences
            ],
        },
        "agent_memory": {
            "capability_invocations": audit_total,
            "failed_invocations": audit_fail,
            "tool_usage_errors": tool_errors,
            "top_capabilities": [
                {"capability_id": cid, "count": cnt} for cid, cnt in rows if cid
            ],
        },
        "system_memory": {
            "capability_gaps_and_data_gaps": gaps,
            "change_requests": cr_by_status,
        },
    }