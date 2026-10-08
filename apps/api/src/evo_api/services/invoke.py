from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from evo_api.models.entities import (
    AgentRun,
    AuditLog,
    Capability,
    CapabilityStatus,
    ChangeRequest,
    ChangeRequestStatus,
    InvestmentDecision,
    IssueType,
    Observation,
    Outcome,
    Review,
    Thesis,
)
from evo_api.services import portfolio as portfolio_svc
from market_data.provider import MarketDataError, get_provider


class CapabilityError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise CapabilityError(f"missing required field: {key}")
    return payload[key]


def invoke_capability(
    db: Session,
    capability_id: str,
    payload: dict[str, Any] | None = None,
    *,
    actor: str = "agent",
) -> dict[str, Any]:
    payload = payload or {}
    cap = db.get(Capability, capability_id)
    if not cap or cap.status != CapabilityStatus.active:
        raise CapabilityError(f"capability not found or inactive: {capability_id}", 404)

    # Minimal required-field check from JSON schema
    required = (cap.input_schema or {}).get("required") or []
    for key in required:
        if key not in payload:
            raise CapabilityError(f"invalid input: missing {key}")

    try:
        result = _dispatch(db, cap.implementation or capability_id, payload)
        success = True
        error = None
    except MarketDataError as exc:
        result = {"error": str(exc), "issue_hint": "DataGap"}
        success = False
        error = str(exc)
    except CapabilityError:
        raise
    except ValueError as exc:
        raise CapabilityError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise CapabilityError(str(exc), 500) from exc

    db.add(
        AuditLog(
            actor=actor,
            capability_id=capability_id,
            action="invoke",
            request=payload,
            response=result if isinstance(result, dict) else {"result": result},
            success=success,
            error=error,
        )
    )
    db.commit()
    if not success:
        raise CapabilityError(error or "capability failed", 502)
    return result


def _dispatch(db: Session, impl: str, payload: dict[str, Any]) -> dict[str, Any]:
    if impl == "list_capabilities":
        rows = (
            db.query(Capability)
            .filter(Capability.status == CapabilityStatus.active)
            .order_by(Capability.id)
            .all()
        )
        return {
            "capabilities": [
                {
                    "id": r.id,
                    "name": r.name,
                    "description": r.description,
                    "category": r.category,
                    "version": r.version,
                    "input_schema": r.input_schema,
                    "permission": r.permission,
                }
                for r in rows
            ]
        }

    if impl == "get_market_snapshot":
        codes = _require(payload, "codes")
        data = get_provider().get_market_snapshot(list(codes))
        return {"quotes": data}

    if impl == "get_etf_history":
        code = _require(payload, "code")
        rows = get_provider().get_etf_history(
            code,
            count=int(payload.get("count", 60)),
            period=str(payload.get("period", "day")),
            adjust=str(payload.get("adjust", "qfq")),
        )
        return {"code": code, "bars": rows}

    if impl == "get_trading_calendar":
        year = int(_require(payload, "year"))
        month = int(_require(payload, "month"))
        return {"year": year, "month": month, "days": get_provider().get_trading_calendar(year, month)}

    if impl == "submit_observation":
        obs = Observation(
            content=_require(payload, "content"),
            data=payload.get("data") or {},
            agent_run_id=payload.get("agent_run_id"),
        )
        db.add(obs)
        db.flush()
        return {"observation_id": obs.id}

    if impl == "submit_thesis":
        th = Thesis(
            content=_require(payload, "content"),
            observation_id=payload.get("observation_id"),
            agent_run_id=payload.get("agent_run_id"),
        )
        db.add(th)
        db.flush()
        return {"thesis_id": th.id}

    if impl == "submit_decision":
        dec = InvestmentDecision(
            summary=_require(payload, "summary"),
            hypothesis=payload.get("hypothesis") or "",
            action_plan=payload.get("action_plan") or "",
            observation_id=payload.get("observation_id"),
            thesis_id=payload.get("thesis_id"),
            agent_run_id=payload.get("agent_run_id"),
        )
        db.add(dec)
        db.flush()
        return {"decision_id": dec.id}

    if impl == "submit_order":
        return portfolio_svc.submit_order(
            db,
            symbol=_require(payload, "symbol"),
            side=_require(payload, "side"),
            quantity=float(_require(payload, "quantity")),
            decision_id=payload.get("decision_id"),
            agent_run_id=payload.get("agent_run_id"),
        )

    if impl == "get_portfolio":
        return portfolio_svc.portfolio_view(db)

    if impl == "submit_review":
        issue_type = IssueType(_require(payload, "issue_type"))
        create_cr = bool(payload.get("create_change_request"))
        if issue_type in (IssueType.CapabilityGap, IssueType.DataGap) and create_cr is False:
            # auto-suggest CR for gaps when agent forgot the flag but provided change_request body
            if payload.get("change_request"):
                create_cr = True
        cr_id = None
        if create_cr:
            if issue_type not in (IssueType.CapabilityGap, IssueType.DataGap):
                raise CapabilityError(
                    "change requests only allowed for CapabilityGap or DataGap"
                )
            cr_body = payload.get("change_request") or {
                "title": f"{issue_type.value}: auto from review",
                "problem": payload.get("content"),
                "issue_type": issue_type.value,
            }
            cr = ChangeRequest(
                title=cr_body.get("title") or "Untitled",
                problem=cr_body.get("problem") or payload["content"],
                evidence=cr_body.get("evidence") or "",
                proposal=cr_body.get("proposal") or "",
                expected_benefit=cr_body.get("expected_benefit") or "",
                issue_type=issue_type,
            )
            db.add(cr)
            db.flush()
            cr_id = cr.id
        review = Review(
            decision_id=payload.get("decision_id"),
            agent_run_id=payload.get("agent_run_id"),
            issue_type=issue_type,
            content=_require(payload, "content"),
            create_change_request=create_cr,
            change_request_id=cr_id,
        )
        db.add(review)
        db.flush()
        # lightweight outcome link
        if payload.get("decision_id"):
            db.add(
                Outcome(
                    decision_id=payload["decision_id"],
                    summary=f"Reviewed as {issue_type.value}",
                    metrics={"review_id": review.id},
                )
            )
        return {"review_id": review.id, "change_request_id": cr_id}

    if impl == "create_change_request":
        issue_type = IssueType(_require(payload, "issue_type"))
        if issue_type not in (IssueType.CapabilityGap, IssueType.DataGap):
            raise CapabilityError("issue_type must be CapabilityGap or DataGap")
        cr = ChangeRequest(
            title=_require(payload, "title"),
            problem=_require(payload, "problem"),
            evidence=payload.get("evidence") or "",
            proposal=payload.get("proposal") or "",
            expected_benefit=payload.get("expected_benefit") or "",
            issue_type=issue_type,
        )
        db.add(cr)
        db.flush()
        return {"change_request_id": cr.id, "status": cr.status.value}

    if impl == "list_change_requests":
        q = db.query(ChangeRequest).order_by(ChangeRequest.id.desc())
        if payload.get("status"):
            q = q.filter(ChangeRequest.status == ChangeRequestStatus(payload["status"]))
        rows = q.limit(100).all()
        return {
            "items": [
                {
                    "id": r.id,
                    "title": r.title,
                    "problem": r.problem,
                    "issue_type": r.issue_type.value,
                    "status": r.status.value,
                    "proposal": r.proposal,
                    "implemented_capability_id": r.implemented_capability_id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        }

    if impl == "start_agent_run":
        run = AgentRun(trigger=payload.get("trigger") or "manual", notes=payload.get("notes"))
        db.add(run)
        db.flush()
        return {"agent_run_id": run.id, "status": run.status}

    if impl == "finish_agent_run":
        run = db.get(AgentRun, _require(payload, "agent_run_id"))
        if not run:
            raise CapabilityError("agent_run not found", 404)
        run.status = payload.get("status") or "completed"
        run.notes = payload.get("notes") or run.notes
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        return {"agent_run_id": run.id, "status": run.status}

    if impl == "noop":
        return {"status": "noop", "echo": payload}

    raise CapabilityError(f"no implementation for {impl}", 500)