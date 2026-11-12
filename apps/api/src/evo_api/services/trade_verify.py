"""成交验证条件：结构化校验、到期排队、客观指标评估。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from evo_api.models.entities import Position, Trade, TradeVerifyStatus
from evo_api.services import market_session as market_session_svc

VERIFY_METRICS = frozenset({"return_vs_cost", "return_vs_benchmark", "still_held"})
VERIFY_OPS = frozenset({"gte", "lte", "gt", "lt"})


def parse_verify_spec(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("verify 必须是对象")
    try:
        horizon = int(raw.get("horizon_days"))
    except (TypeError, ValueError) as exc:
        raise ValueError("verify.horizon_days 必须为正整数") from exc
    if horizon < 1 or horizon > 252:
        raise ValueError("verify.horizon_days 须在 1–252")
    metric = str(raw.get("metric") or "").strip()
    if metric not in VERIFY_METRICS:
        raise ValueError(
            "verify.metric 须为 return_vs_cost | return_vs_benchmark | still_held"
        )
    op = str(raw.get("op") or "").strip()
    if op not in VERIFY_OPS:
        raise ValueError("verify.op 须为 gte | lte | gt | lt")
    try:
        target = float(raw.get("target"))
    except (TypeError, ValueError) as exc:
        raise ValueError("verify.target 必须是数值") from exc
    summary = str(raw.get("summary") or "").strip()
    if not summary:
        raise ValueError("verify.summary 必填（人话描述验证条件）")
    benchmark = raw.get("benchmark")
    if metric == "return_vs_benchmark":
        bmk = "".join(ch for ch in str(benchmark or "") if ch.isdigit())[-6:]
        if len(bmk) != 6:
            raise ValueError("return_vs_benchmark 需要 6 位 benchmark 代码")
        benchmark = bmk
    else:
        benchmark = (
            "".join(ch for ch in str(benchmark) if ch.isdigit())[-6:]
            if benchmark
            else None
        ) or None
    return {
        "horizon_days": horizon,
        "metric": metric,
        "op": op,
        "target": target,
        "benchmark": benchmark,
        "summary": summary,
    }


def parse_order_evidence(
    *,
    rationale: Any,
    confidence: Any,
    verify: Any,
) -> dict[str, Any]:
    text = str(rationale or "").strip()
    if len(text) < 8:
        raise ValueError("rationale（决策依据）必填，至少 8 个字符")
    try:
        conf = float(confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence 须为 0–100 数值") from exc
    if conf < 0 or conf > 100:
        raise ValueError("confidence 须在 0–100")
    spec = parse_verify_spec(verify)
    return {"rationale": text, "confidence": round(conf, 1), "verify_spec": spec}


def add_trading_days(start: date, n: int) -> date:
    """从 start 之后数 n 个交易日（不含 start 当日）。"""
    if n <= 0:
        return start
    d = start
    added = 0
    guard = 0
    while added < n and guard < max(n * 5, 40):
        d += timedelta(days=1)
        guard += 1
        try:
            if market_session_svc.is_trading_day(d):
                added += 1
                continue
        except Exception:  # noqa: BLE001
            pass
        # 日历失败时：工作日近似
        if d.weekday() < 5:
            added += 1
    return d


def _compare(op: str, value: float, target: float) -> bool:
    if op == "gte":
        return value >= target
    if op == "lte":
        return value <= target
    if op == "gt":
        return value > target
    if op == "lt":
        return value < target
    raise ValueError(f"未知 op: {op}")


def _bar_close_on_or_after(bars: list[dict[str, Any]], day: date) -> float | None:
    for bar in bars:
        raw = str(bar.get("date") or "")[:10]
        try:
            bd = date.fromisoformat(raw)
        except ValueError:
            continue
        if bd >= day:
            px = bar.get("close") if bar.get("close") is not None else bar.get("price")
            try:
                return float(px)
            except (TypeError, ValueError):
                return None
    return None


def _bar_close_last(bars: list[dict[str, Any]]) -> float | None:
    if not bars:
        return None
    bar = bars[-1]
    px = bar.get("close") if bar.get("close") is not None else bar.get("price")
    try:
        return float(px)
    except (TypeError, ValueError):
        return None


def evaluate_trade(db: Session, trade: Trade, *, as_of: date | None = None) -> dict[str, Any]:
    """计算客观指标；不写库。"""
    from evo_api.services.portfolio import get_price
    from market_data.provider import get_provider

    as_of = as_of or market_session_svc.now_cn().date()
    spec = trade.verify_spec or {}
    metric = str(spec.get("metric") or "")
    op = str(spec.get("op") or "")
    try:
        target = float(spec.get("target"))
    except (TypeError, ValueError):
        return {
            "evaluable": False,
            "condition_met": None,
            "reason": "verify_spec.target 无效",
            "as_of": as_of.isoformat(),
        }
    entry = float(trade.price)
    if trade.created_at is None:
        trade_day = market_session_svc.now_cn().date()
    elif trade.created_at.tzinfo:
        trade_day = trade.created_at.astimezone(market_session_svc.CN_TZ).date()
    else:
        trade_day = trade.created_at.date()
    out: dict[str, Any] = {
        "evaluable": True,
        "metric": metric,
        "op": op,
        "target": target,
        "entry_price": entry,
        "trade_date": trade_day.isoformat(),
        "as_of": as_of.isoformat(),
        "side": trade.side.value,
        "symbol": trade.symbol,
    }
    try:
        if metric == "still_held":
            pos = db.query(Position).filter_by(symbol=trade.symbol).one_or_none()
            qty = float(pos.quantity) if pos else 0.0
            observed = 1.0 if qty > 1e-9 else 0.0
            out["observed"] = observed
            out["position_quantity"] = qty
            out["condition_met"] = _compare(op, observed, target)
            return out

        current = float(get_price(trade.symbol))
        out["current_price"] = current
        if entry <= 0:
            return {
                **out,
                "evaluable": False,
                "condition_met": None,
                "reason": "成交价无效",
            }
        ret_cost = (current - entry) / entry
        out["return_vs_cost"] = round(ret_cost, 6)

        if metric == "return_vs_cost":
            out["observed"] = out["return_vs_cost"]
            out["condition_met"] = _compare(op, ret_cost, target)
            return out

        if metric == "return_vs_benchmark":
            bmk = str(spec.get("benchmark") or "")
            provider = get_provider()
            # 多取一些 bar，覆盖 horizon
            need = max(int(spec.get("horizon_days") or 5) + 5, 15)
            sym_bars = provider.get_etf_history(trade.symbol, count=need + 40)
            bmk_bars = provider.get_etf_history(bmk, count=need + 40)
            # history 通常按时间升序；截到 as_of
            def _upto(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
                kept = []
                for bar in bars or []:
                    raw = str(bar.get("date") or "")[:10]
                    try:
                        if date.fromisoformat(raw) <= as_of:
                            kept.append(bar)
                    except ValueError:
                        continue
                return kept

            sym_bars = _upto(list(sym_bars or []))
            bmk_bars = _upto(list(bmk_bars or []))
            bmk_entry = _bar_close_on_or_after(bmk_bars, trade_day)
            bmk_now = _bar_close_last(bmk_bars)
            if bmk_entry is None or bmk_now is None or bmk_entry <= 0:
                return {
                    **out,
                    "evaluable": False,
                    "condition_met": None,
                    "reason": f"基准 {bmk} 历史价不可用",
                    "benchmark": bmk,
                }
            ret_bmk = (bmk_now - bmk_entry) / bmk_entry
            alpha = ret_cost - ret_bmk
            out["benchmark"] = bmk
            out["benchmark_entry"] = bmk_entry
            out["benchmark_current"] = bmk_now
            out["return_vs_benchmark_raw"] = round(ret_bmk, 6)
            out["observed"] = round(alpha, 6)
            out["condition_met"] = _compare(op, alpha, target)
            return out

        return {
            **out,
            "evaluable": False,
            "condition_met": None,
            "reason": f"未知 metric: {metric}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            **out,
            "evaluable": False,
            "condition_met": None,
            "reason": str(exc)[:300],
        }


def _trade_payload(t: Trade, *, eval_now: bool = False, db: Session | None = None) -> dict[str, Any]:
    today = market_session_svc.now_cn().date()
    due = t.verify_due_date
    status = (
        t.verify_status.value
        if hasattr(t.verify_status, "value")
        else t.verify_status
    )
    is_due = bool(status == "pending" and due is not None and due <= today)
    item: dict[str, Any] = {
        "id": t.id,
        "order_id": t.order_id,
        "symbol": t.symbol,
        "side": t.side.value,
        "quantity": t.quantity,
        "price": t.price,
        "rationale": t.rationale,
        "confidence": t.confidence,
        "verify_spec": t.verify_spec,
        "verify_due_date": due.isoformat() if due else None,
        "verify_status": status,
        "is_due": is_due,
        "verify_eval": t.verify_eval,
        "verify_outcome": t.verify_outcome,
        "verify_lesson": t.verify_lesson,
        "verified_at": t.verified_at.isoformat() if t.verified_at else None,
        "verified_review_id": t.verified_review_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "trade_date": (
            t.created_at.astimezone(market_session_svc.CN_TZ).date().isoformat()
            if t.created_at and t.created_at.tzinfo
            else (t.created_at.date().isoformat() if t.created_at else None)
        ),
    }
    if eval_now and db is not None and t.verify_spec and status == "pending":
        item["live_eval"] = evaluate_trade(db, t)
    return item


def _status_str(value: Any) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def list_trade_verifications(
    db: Session,
    *,
    status: str = "due",
    limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    status = (status or "due").strip().lower()
    if status not in {"due", "pending", "verified", "all"}:
        raise ValueError("status 须为 due|pending|verified|all")
    q = db.query(Trade).filter(Trade.verify_spec.isnot(None))
    today = market_session_svc.now_cn().date()
    rows = q.order_by(Trade.id.desc()).limit(limit * 5).all()
    items: list[dict[str, Any]] = []
    for t in rows:
        st = _status_str(t.verify_status)
        due = t.verify_due_date
        is_due = st == "pending" and due is not None and due <= today
        if status == "due" and not is_due:
            continue
        if status == "pending" and st != "pending":
            continue
        if status == "verified" and st != "verified":
            continue
        items.append(_trade_payload(t, eval_now=(status == "due"), db=db))
        if len(items) >= limit:
            break
    return {"count": len(items), "status": status, "as_of": today.isoformat(), "items": items}


def submit_trade_verification(
    db: Session,
    *,
    trade_id: int,
    outcome: str,
    lesson: str | None = None,
    review_id: int | None = None,
    skip: bool = False,
) -> dict[str, Any]:
    trade = db.get(Trade, trade_id)
    if not trade:
        raise ValueError(f"未找到成交 {trade_id}")
    if not trade.verify_spec:
        raise ValueError("该成交无验证条件，无法验收")
    if _status_str(trade.verify_status) == "verified":
        raise ValueError("该成交已验收，勿重复提交")
    text = str(outcome or "").strip()
    if len(text) < 4 and not skip:
        raise ValueError("outcome（验证结论）必填")
    if skip:
        trade.verify_status = TradeVerifyStatus.skipped.value
        trade.verify_outcome = text or "跳过验收"
        trade.verify_lesson = (str(lesson).strip() if lesson else None) or None
        trade.verified_at = market_session_svc.now_cn()
        trade.verified_review_id = review_id
        trade.verify_eval = {"skipped": True}
        db.flush()
        return _trade_payload(trade)
    ev = evaluate_trade(db, trade)
    trade.verify_eval = ev
    trade.verify_outcome = text
    trade.verify_lesson = (str(lesson).strip() if lesson else None) or None
    trade.verify_status = TradeVerifyStatus.verified.value
    trade.verified_at = market_session_svc.now_cn()
    trade.verified_review_id = review_id
    db.flush()
    return _trade_payload(trade)
