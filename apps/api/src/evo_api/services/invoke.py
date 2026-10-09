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
    Experience,
    ExperienceKind,
    InvestmentDecision,
    IssueType,
    Observation,
    Outcome,
    Review,
    Thesis,
)
from evo_api.services import portfolio as portfolio_svc
from evo_api.services.market_session import (
    CN_TZ,
    require_review_window,
    require_trading_session,
    session_snapshot,
)
from market_data.provider import MarketDataError, get_provider


class CapabilityError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise CapabilityError(f"缺少必填字段：{key}")
    return payload[key]


def _market_soft(fn, fallback: dict[str, Any]) -> dict[str, Any]:
    """东财等分项失败时返回降级结果，避免聚合/板块能力整包 502。"""
    try:
        data = fn()
        return data if isinstance(data, dict) else {"result": data}
    except MarketDataError as exc:
        out = dict(fallback)
        out["error"] = str(exc)
        out["degraded"] = True
        return out


def _shanghai_date(value: datetime | None) -> Any:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CN_TZ).date()


def _cr_item(row: ChangeRequest) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "problem": row.problem,
        "evidence": row.evidence,
        "proposal": row.proposal,
        "expected_benefit": row.expected_benefit,
        "issue_type": row.issue_type.value,
        "status": row.status.value,
        "review_notes": row.review_notes,
        "verification_notes": row.verification_notes,
        "implemented_capability_id": row.implemented_capability_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _save_lessons(
    db: Session,
    lessons: Any,
    *,
    review_id: int | None,
    agent_run_id: int | None,
) -> list[int]:
    if not lessons:
        return []
    require_review_window()
    ids: list[int] = []
    for item in lessons:
        if isinstance(item, str):
            kind = ExperienceKind.investment
            content = item.strip()
        elif isinstance(item, dict):
            kind_raw = item.get("kind") or "investment"
            kind = ExperienceKind(kind_raw)
            content = str(item.get("content") or "").strip()
        else:
            continue
        if not content:
            continue
        exp = Experience(
            kind=kind,
            content=content,
            source_review_id=review_id,
            agent_run_id=agent_run_id,
        )
        db.add(exp)
        db.flush()
        ids.append(exp.id)
    return ids


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
        raise CapabilityError(f"能力不存在或未启用：{capability_id}", 404)

    # Minimal required-field check from JSON schema
    required = (cap.input_schema or {}).get("required") or []
    for key in required:
        if key not in payload:
            raise CapabilityError(f"输入无效：缺少字段 {key}")

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

    run_id = payload.get("agent_run_id")
    if run_id is None and isinstance(result, dict):
        run_id = result.get("agent_run_id")
    try:
        run_id_int = int(run_id) if run_id is not None else None
    except (TypeError, ValueError):
        run_id_int = None

    db.add(
        AuditLog(
            actor=actor,
            capability_id=capability_id,
            agent_run_id=run_id_int,
            action="invoke",
            request=payload,
            response=result if isinstance(result, dict) else {"result": result},
            success=success,
            error=error,
        )
    )
    db.commit()
    if not success:
        raise CapabilityError(error or "能力调用失败", 502)
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
            count=int(payload.get("count", 20)),
            period=str(payload.get("period", "day")),
            adjust=str(payload.get("adjust", "qfq")),
        )
        latest = rows[-1] if rows else None
        return {
            "code": code,
            "count": len(rows),
            "order": "asc",
            "latest": latest,
            "bars": rows,
        }

    if impl == "get_trading_calendar":
        year = int(_require(payload, "year"))
        month = int(_require(payload, "month"))
        return {"year": year, "month": month, "days": get_provider().get_trading_calendar(year, month)}

    if impl == "list_universe":
        uni = portfolio_svc.universe_config(payload.get("stock_type"))
        stock_type = uni["stock_type"]
        index_code = str(payload.get("index_code") or uni.get("index_code") or "000300")
        members = get_provider().list_universe(
            stock_type=stock_type,
            index_code=index_code,
            seed_codes=uni.get("seed_codes"),
        )
        return {
            "stock_type": stock_type,
            "index_code": index_code if stock_type == "stock" else None,
            "count": len(members),
            "members": members,
        }

    if impl == "screen_market":
        uni = portfolio_svc.universe_config(payload.get("stock_type"))
        stock_type = uni["stock_type"]
        index_code = str(payload.get("index_code") or uni.get("index_code") or "000300")
        top_n = int(payload.get("top_n") or 30)
        extra = payload.get("extra_codes") or []
        data = get_provider().screen_market(
            stock_type=stock_type,
            index_code=index_code,
            top_n=top_n,
            extra_codes=list(extra),
            seed_codes=uni.get("seed_codes"),
        )
        return data

    if impl == "get_market_overview":
        return _market_soft(
            lambda: get_provider().get_market_overview(top_n=int(payload.get("top_n") or 8)),
            {
                "as_of": datetime.now().isoformat(timespec="seconds"),
                "indices": [],
                "industry": {"leaders": [], "laggards": [], "total": 0},
                "concept": {"hot": [], "cold": [], "total": 0},
                "industry_fund_flow": {"period": "today", "inflow": [], "outflow": []},
                "gaps": ["大盘综合上游失败，已降级"],
            },
        )

    if impl == "get_industry_ranking":
        board = str(payload.get("board") or "industry")
        return _market_soft(
            lambda: get_provider().industry_comparison(
                top_n=int(payload.get("top_n") or 10),
                board=board,
            ),
            {"board": board, "top": [], "bottom": [], "total": 0},
        )

    if impl == "get_board_fund_flow":
        board_type = str(payload.get("board_type") or "industry")
        period = str(payload.get("period") or "today")
        return _market_soft(
            lambda: get_provider().board_fund_flow(
                board_type=board_type,
                period=period,
                top_n=int(payload.get("top_n") or 10),
            ),
            {
                "board_type": board_type,
                "period": period,
                "total": 0,
                "inflow": [],
                "outflow": [],
            },
        )

    if impl == "get_index_valuation":
        code = str(_require(payload, "code"))
        return _market_soft(
            lambda: get_provider().get_index_valuation(code),
            {"code": code, "pe": None, "pb": None, "pe_ttm": None},
        )

    if impl == "get_market_breadth":
        return _market_soft(
            lambda: get_provider().get_market_breadth(),
            {"up": 0, "down": 0, "flat": 0, "total": 0, "histogram": []},
        )

    if impl == "get_market_session":
        info = session_snapshot()
        today = datetime.fromisoformat(info["now"]).date()
        done = False
        runs = db.query(AgentRun).filter(AgentRun.trigger == "postclose").all()
        for run in runs:
            run_day = _shanghai_date(run.finished_at or run.created_at)
            if run_day == today and (run.status or "") in ("completed", "ok"):
                done = True
                break
        info["postclose_completed_today"] = done
        return info

    if impl == "take_portfolio_snapshot":
        snap = portfolio_svc.take_snapshot(db)
        return {
            "snapshot_id": snap.id,
            "cash": snap.cash,
            "equity": snap.equity,
            "positions": snap.positions,
        }

    if impl == "settle_day":
        return portfolio_svc.settle_day(db)

    if impl == "get_day_report":
        return portfolio_svc.day_report(db)

    if impl == "list_today_decisions":
        return portfolio_svc.list_today_decisions(db)

    if impl == "list_today_orders":
        return portfolio_svc.list_today_orders(db)

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
        usage_notes = payload.get("usage_notes") or []
        if not isinstance(usage_notes, list):
            usage_notes = []
        cleaned_notes = []
        for item in usage_notes:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "other")
            if kind not in {"missing_tool", "tool_error", "tool_improve", "other"}:
                kind = "other"
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            note = {"kind": kind, "content": content}
            cap = str(item.get("capability_id") or "").strip()
            if cap:
                note["capability_id"] = cap
            cleaned_notes.append(note)
        dec = InvestmentDecision(
            summary=_require(payload, "summary"),
            hypothesis=payload.get("hypothesis") or "",
            action_plan=payload.get("action_plan") or "",
            usage_notes=cleaned_notes,
            observation_id=payload.get("observation_id"),
            thesis_id=payload.get("thesis_id"),
            agent_run_id=payload.get("agent_run_id"),
        )
        db.add(dec)
        db.flush()
        return {"decision_id": dec.id, "usage_notes": cleaned_notes}

    if impl == "submit_order":
        require_trading_session()
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
        if create_cr or payload.get("lessons"):
            require_review_window()
        cr_id = None
        if create_cr:
            if issue_type not in (IssueType.CapabilityGap, IssueType.DataGap):
                raise CapabilityError(
                    "仅 CapabilityGap 或 DataGap 可创建变更请求"
                )
            cr_body = payload.get("change_request") or {
                "title": f"{issue_type.value}：来自复盘的自动变更请求",
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
        lesson_ids = _save_lessons(
            db,
            payload.get("lessons"),
            review_id=review.id,
            agent_run_id=payload.get("agent_run_id"),
        )
        # lightweight outcome link
        if payload.get("decision_id"):
            db.add(
                Outcome(
                    decision_id=payload["decision_id"],
                    summary=f"已复盘，问题类型：{issue_type.value}",
                    metrics={"review_id": review.id},
                )
            )
        return {
            "review_id": review.id,
            "change_request_id": cr_id,
            "experience_ids": lesson_ids,
        }

    if impl == "create_change_request":
        require_review_window()
        issue_type = IssueType(_require(payload, "issue_type"))
        if issue_type not in (IssueType.CapabilityGap, IssueType.DataGap):
            raise CapabilityError("issue_type 只能是 CapabilityGap 或 DataGap")
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
        return {"items": [_cr_item(r) for r in rows]}

    if impl == "list_experiences":
        q = db.query(Experience).order_by(Experience.id.desc())
        if payload.get("kind"):
            q = q.filter(Experience.kind == ExperienceKind(payload["kind"]))
        limit = min(int(payload.get("limit") or 20), 100)
        rows = q.limit(limit).all()
        return {
            "items": [
                {
                    "id": r.id,
                    "kind": r.kind.value,
                    "content": r.content,
                    "source_review_id": r.source_review_id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        }

    if impl == "verify_change_request":
        require_review_window()
        cr = db.get(ChangeRequest, int(_require(payload, "change_request_id")))
        if not cr:
            raise CapabilityError("未找到变更请求", 404)
        if cr.status != ChangeRequestStatus.completed:
            raise CapabilityError(f"仅已完成的 CR 可验收，当前状态：{cr.status.value}")
        passed = bool(payload.get("passed"))
        evidence = str(payload.get("evidence") or "").strip()
        cr.verification_notes = evidence or cr.verification_notes
        if passed:
            cr.status = ChangeRequestStatus.verified
        else:
            cr.status = ChangeRequestStatus.pending_dev
            reason = evidence or "验收未通过"
            note = f"[验收未通过] {reason}"
            cr.review_notes = f"{cr.review_notes}\n{note}" if cr.review_notes else note
        db.flush()
        return {"change_request_id": cr.id, "status": cr.status.value, "passed": passed}

    if impl == "start_agent_run":
        run = AgentRun(trigger=payload.get("trigger") or "manual", notes=payload.get("notes"))
        db.add(run)
        db.flush()
        return {"agent_run_id": run.id, "status": run.status}

    if impl == "finish_agent_run":
        run = db.get(AgentRun, _require(payload, "agent_run_id"))
        if not run:
            raise CapabilityError("找不到对应的 AgentRun", 404)
        run.status = payload.get("status") or "completed"
        run.notes = payload.get("notes") or run.notes
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        return {"agent_run_id": run.id, "status": run.status}

    if impl == "noop":
        return {"status": "noop", "echo": payload}

    raise CapabilityError(f"未实现的能力：{impl}", 500)