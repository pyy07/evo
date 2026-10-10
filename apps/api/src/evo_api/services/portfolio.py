from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from evo_api.config import get_settings, load_instruments
from evo_api.models.entities import (
    AgentRun,
    AuditLog,
    Settlement,
    Order,
    OrderSide,
    OrderStatus,
    PortfolioAccount,
    PortfolioSnapshot,
    Position,
    Trade,
)
from evo_api.services import market_session as market_session_svc
from market_data.provider import MarketDataError, get_provider

CN_TZ = market_session_svc.CN_TZ


def _to_cn_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(CN_TZ).date()


def _today_cn() -> date:
    return market_session_svc.now_cn().astimezone(CN_TZ).date()


def bought_today_quantity(db: Session, symbol: str, *, today: date | None = None) -> float:
    """Net buy quantity booked on the Shanghai calendar day (T+1 lock)."""
    day = today or _today_cn()
    trades = (
        db.query(Trade)
        .filter(Trade.symbol == symbol)
        .order_by(Trade.id.asc())
        .all()
    )
    bought = 0.0
    for t in trades:
        if _to_cn_date(t.created_at) != day:
            continue
        if t.side == OrderSide.buy:
            bought += float(t.quantity)
        elif t.side == OrderSide.sell:
            # selling same-day buys is blocked; ignore for lock math
            pass
    return bought


def sellable_quantity(db: Session, symbol: str, quantity: float, *, today: date | None = None) -> float:
    """Shares eligible to sell under A-share T+1 (exclude today's buys)."""
    locked = bought_today_quantity(db, symbol, today=today)
    return max(0.0, float(quantity) - locked)


def ensure_account(db: Session) -> PortfolioAccount:
    acct = db.query(PortfolioAccount).filter_by(name="default").one_or_none()
    if acct:
        return acct
    settings = get_settings()
    instruments = load_instruments()
    cash = float(instruments.get("default_cash", settings.initial_cash))
    acct = PortfolioAccount(name="default", cash=cash, currency="CNY", paper=True)
    db.add(acct)
    db.flush()
    return acct


_INDEX_CODES = {
    "000300",
    "000905",
    "000688",
    "000852",
    "000010",
    "399001",
    "399006",
    "399300",
}


def _digits6(symbol: str) -> str:
    digits = "".join(ch for ch in symbol if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _matches_prefixes(code: str, kind: str) -> bool:
    prefixes = load_instruments().get("kinds", {}).get(kind, {}).get("code_prefixes", [])
    return any(code.startswith(p) for p in prefixes)


def looks_like_etf(symbol: str) -> bool:
    code = _digits6(symbol)
    return len(code) >= 6 and _matches_prefixes(code, "etf")


def looks_like_convertible_bond(symbol: str) -> bool:
    code = _digits6(symbol)
    return len(code) >= 6 and _matches_prefixes(code, "convertible_bond")


def looks_like_stock(symbol: str) -> bool:
    code = _digits6(symbol)
    if len(code) < 6 or code in _INDEX_CODES:
        return False
    # avoid classifying ETF/CB as stock when prefixes overlap
    if looks_like_etf(symbol) or looks_like_convertible_bond(symbol):
        return False
    return _matches_prefixes(code, "stock")


def instrument_kind(symbol: str) -> str | None:
    # more specific first: CB / ETF before stock
    if looks_like_convertible_bond(symbol):
        return "convertible_bond"
    if looks_like_etf(symbol):
        return "etf"
    if looks_like_stock(symbol):
        return "stock"
    return None


def configured_stock_type() -> str:
    env = os.getenv("STOCK_TYPE", "").strip()
    if env:
        return env
    return str(load_instruments().get("default_stock_type") or "etf")


def universe_config(stock_type: str | None = None) -> dict[str, Any]:
    st = str(stock_type or "").strip() or configured_stock_type()
    kinds = load_instruments().get("kinds", {})
    if st not in kinds:
        raise ValueError(
            f"未知 stock_type={st!r}；可选：{', '.join(sorted(kinds)) or 'stock/etf/convertible_bond'}"
        )
    kind_cfg = kinds.get(st) or {}
    uni = dict(kind_cfg.get("universe") or {})
    if st == "stock":
        uni.setdefault(
            "index_code",
            os.getenv("UNIVERSE_INDEX")
            or load_instruments().get("universe_index")
            or "000300",
        )
    uni["stock_type"] = st
    uni["seed_codes"] = uni.get("seed_codes") or []
    return uni


def kind_rules(kind: str) -> dict[str, Any]:
    return load_instruments().get("kinds", {}).get(kind, {})


def etf_rules() -> dict[str, Any]:
    return kind_rules("etf")


def get_price(symbol: str) -> float:
    provider = get_provider()
    try:
        quotes = provider.get_market_snapshot([symbol])
    except MarketDataError as exc:
        raise ValueError(f"行情不可用：{exc}") from exc
    q = quotes.get(symbol) or next(iter(quotes.values()), None)
    if not q:
        raise ValueError(f"找不到报价：{symbol}")
    for key in ("price", "current", "last", "close"):
        if key in q and q[key] is not None:
            return float(q[key])
    raise ValueError(f"报价缺少价格字段：{symbol} → {q}")


def portfolio_view(db: Session) -> dict[str, Any]:
    acct = ensure_account(db)
    settings = get_settings()
    instruments = load_instruments()
    initial_cash = float(instruments.get("default_cash", settings.initial_cash))
    positions = db.query(Position).filter(Position.quantity != 0).all()
    marked: list[dict[str, Any]] = []
    equity = acct.cash
    unrealized = 0.0
    cost_basis = 0.0
    for p in positions:
        try:
            px = get_price(p.symbol)
        except ValueError:
            px = p.avg_cost
        mv = px * p.quantity
        cost = p.avg_cost * p.quantity
        pnl = mv - cost
        equity += mv
        unrealized += pnl
        cost_basis += cost
        locked = bought_today_quantity(db, p.symbol)
        sellable = sellable_quantity(db, p.symbol, p.quantity)
        marked.append(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "sellable_quantity": sellable,
                "locked_quantity": locked,
                "avg_cost": p.avg_cost,
                "mark_price": px,
                "market_value": mv,
                "cost_basis": cost,
                "unrealized_pnl": pnl,
                "unrealized_pnl_pct": (pnl / cost * 100.0) if cost else 0.0,
            }
        )
    total_pnl = equity - initial_cash
    today = _today_cn()
    snaps = (
        db.query(PortfolioSnapshot)
        .order_by(PortfolioSnapshot.created_at.desc())
        .limit(500)
        .all()
    )
    prior = [s for s in snaps if _to_cn_date(s.created_at) and _to_cn_date(s.created_at) < today]
    same_day = [
        s for s in snaps if _to_cn_date(s.created_at) and _to_cn_date(s.created_at) == today
    ]
    baseline = prior[0] if prior else (same_day[-1] if same_day else None)
    day_start_equity = float(baseline.equity) if baseline else initial_cash
    day_pnl = equity - day_start_equity
    return {
        "cash": acct.cash,
        "equity": equity,
        "currency": acct.currency,
        "paper": acct.paper,
        "initial_cash": initial_cash,
        "cost_basis": cost_basis,
        "unrealized_pnl": unrealized,
        "total_pnl": total_pnl,
        "total_pnl_pct": (total_pnl / initial_cash * 100.0) if initial_cash else 0.0,
        "day_pnl": day_pnl,
        "day_pnl_pct": (day_pnl / day_start_equity * 100.0) if day_start_equity else 0.0,
        "day_start_equity": day_start_equity,
        "positions": marked,
    }


def _audit_item(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "capability_id": row.capability_id,
        "actor": row.actor,
        "agent_run_id": row.agent_run_id,
        "success": row.success,
        "error": row.error,
        "input": row.request or {},
        "output": row.response or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def invocations_for_run(db: Session, run: AgentRun) -> list[dict[str, Any]]:
    """Capability calls belonging to one agent run (by id and time window)."""
    by_id = (
        db.query(AuditLog)
        .filter(AuditLog.agent_run_id == run.id)
        .order_by(AuditLog.id.asc())
        .all()
    )
    start = run.created_at
    if start is None:
        return [_audit_item(r) for r in by_id]
    # include list_capabilities / session calls just before start_agent_run
    window_start = start - timedelta(seconds=45)
    window_end = run.finished_at or (start + timedelta(minutes=20))
    window_rows = (
        db.query(AuditLog)
        .filter(AuditLog.created_at >= window_start, AuditLog.created_at <= window_end)
        .order_by(AuditLog.id.asc())
        .all()
    )
    seen = {r.id for r in by_id}
    merged = list(by_id)
    for row in window_rows:
        if row.id in seen:
            continue
        # skip audits clearly tagged to another run
        if row.agent_run_id is not None and row.agent_run_id != run.id:
            continue
        req_run = (row.request or {}).get("agent_run_id")
        if req_run is not None:
            try:
                if int(req_run) != run.id:
                    continue
            except (TypeError, ValueError):
                pass
        # for untagged window rows, keep market/system reads around this run
        if row.agent_run_id is None and req_run is None:
            if row.capability_id in {
                "list_capabilities",
                "get_market_session",
                "get_market_snapshot",
                "get_etf_history",
                "get_portfolio",
                "list_experiences",
                "list_change_requests",
                "list_universe",
                "screen_market",
                "get_market_overview",
                "get_industry_ranking",
                "get_board_fund_flow",
                "get_index_valuation",
                "get_market_breadth",
                "take_portfolio_snapshot",
                "settle_day",
                "get_day_report",
                "list_today_decisions",
                "list_today_orders",
                "start_agent_run",
                "finish_agent_run",
            } or (isinstance(row.response, dict) and row.response.get("agent_run_id") == run.id):
                merged.append(row)
                seen.add(row.id)
                continue
            continue
        merged.append(row)
        seen.add(row.id)
    merged.sort(key=lambda r: r.id)
    return [_audit_item(r) for r in merged]


def desk_today(db: Session) -> dict[str, Any]:
    """Today's decisions and trades (Asia/Shanghai calendar day)."""
    from evo_api.models.entities import InvestmentDecision

    today = _today_cn()
    decisions = [
        d
        for d in db.query(InvestmentDecision)
        .order_by(InvestmentDecision.created_at.desc())
        .limit(200)
        .all()
        if _to_cn_date(d.created_at) == today
    ]
    orders = [
        o
        for o in db.query(Order).order_by(Order.created_at.desc()).limit(200).all()
        if _to_cn_date(o.created_at) == today
    ]
    trades = [
        t
        for t in db.query(Trade).order_by(Trade.created_at.desc()).limit(200).all()
        if _to_cn_date(t.created_at) == today
    ]
    runs = [
        r
        for r in db.query(AgentRun).order_by(AgentRun.created_at.desc()).limit(200).all()
        if _to_cn_date(r.created_at) == today
    ]
    trade_by_order = {t.order_id: t for t in trades}
    invocations_by_run = {r.id: invocations_for_run(db, r) for r in runs}
    return {
        "date": today.isoformat(),
        "runs": [
            {
                "id": r.id,
                "status": r.status,
                "trigger": r.trigger,
                "notes": r.notes,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "invocations": invocations_by_run.get(r.id, []),
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
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "invocations": invocations_by_run.get(d.agent_run_id, []) if d.agent_run_id else [],
            }
            for d in decisions
        ],
        "orders": [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side.value,
                "quantity": o.quantity,
                "status": o.status.value,
                "reason": o.reason,
                "decision_id": o.decision_id,
                "agent_run_id": o.agent_run_id,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "price": trade_by_order[o.id].price if o.id in trade_by_order else None,
                "commission": (
                    trade_by_order[o.id].commission if o.id in trade_by_order else None
                ),
            }
            for o in orders
        ],
        "trades": [_trade_item(t) for t in trades],
    }


def _trade_item(t: Trade) -> dict[str, Any]:
    return {
        "id": t.id,
        "order_id": t.order_id,
        "symbol": t.symbol,
        "side": t.side.value,
        "quantity": t.quantity,
        "price": t.price,
        "amount": round(float(t.price) * float(t.quantity), 2),
        "commission": t.commission,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "trade_date": (
            _to_cn_date(t.created_at).isoformat() if _to_cn_date(t.created_at) else None
        ),
    }


def list_trades(db: Session, *, limit: int = 200, exclude_today: bool = False) -> dict[str, Any]:
    """成交列表（默认最近 N 笔；exclude_today=True 时仅历史日）。"""
    limit = max(1, min(int(limit), 500))
    today = _today_cn()
    rows = db.query(Trade).order_by(Trade.id.desc()).limit(limit * 3).all()
    items: list[dict[str, Any]] = []
    for t in rows:
        d = _to_cn_date(t.created_at)
        if exclude_today and d == today:
            continue
        items.append(_trade_item(t))
        if len(items) >= limit:
            break
    return {"count": len(items), "items": items}


def take_snapshot(db: Session) -> PortfolioSnapshot:
    view = portfolio_view(db)
    snap = PortfolioSnapshot(
        cash=view["cash"],
        equity=view["equity"],
        positions={p["symbol"]: p for p in view["positions"]},
        created_at=market_session_svc.now_cn(),
    )
    db.add(snap)
    db.flush()
    return snap


def _slim_decisions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in items:
        out.append(
            {
                "id": d.get("id"),
                "summary": d.get("summary"),
                "hypothesis": d.get("hypothesis"),
                "action_plan": d.get("action_plan"),
                "usage_notes": d.get("usage_notes") or [],
                "agent_run_id": d.get("agent_run_id"),
                "created_at": d.get("created_at"),
            }
        )
    return out


def _day_trade_amounts(trades: list[dict[str, Any]]) -> tuple[float, float]:
    buy = 0.0
    sell = 0.0
    for t in trades:
        amount = float(t.get("amount") or (float(t.get("price") or 0) * float(t.get("quantity") or 0)))
        side = t.get("side")
        if side == "buy" or side == OrderSide.buy:
            buy += amount
        elif side == "sell" or side == OrderSide.sell:
            sell += amount
    return round(buy, 2), round(sell, 2)


def _open_cash_baseline(db: Session, trade_date: date, fallback_cash: float) -> float:
    """日初现金：优先前日清算 close_cash，否则前日快照现金。"""
    prior_settle = (
        db.query(Settlement)
        .filter(Settlement.trade_date < trade_date)
        .order_by(Settlement.trade_date.desc())
        .first()
    )
    if prior_settle is not None:
        return float(prior_settle.close_cash)
    snaps = (
        db.query(PortfolioSnapshot)
        .order_by(PortfolioSnapshot.created_at.desc())
        .limit(500)
        .all()
    )
    for s in snaps:
        d = _to_cn_date(s.created_at)
        if d and d < trade_date:
            return float(s.cash)
    return float(fallback_cash)


def _settlement_dict(row: Settlement) -> dict[str, Any]:
    return {
        "id": row.id,
        "trade_date": row.trade_date.isoformat(),
        "open_equity": row.open_equity,
        "close_equity": row.close_equity,
        "open_cash": row.open_cash,
        "close_cash": row.close_cash,
        "day_pnl": row.day_pnl,
        "day_pnl_pct": row.day_pnl_pct,
        "total_pnl": row.total_pnl,
        "total_pnl_pct": row.total_pnl_pct,
        "unrealized_pnl": row.unrealized_pnl,
        "buy_amount": row.buy_amount,
        "sell_amount": row.sell_amount,
        "trade_count": row.trade_count,
        "order_count": row.order_count,
        "decision_count": row.decision_count,
        "positions": row.positions or {},
        "snapshot_id": row.snapshot_id,
        "agent_run_id": row.agent_run_id,
        "notes": row.notes or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def upsert_settlement(
    db: Session,
    *,
    view: dict[str, Any],
    snap: PortfolioSnapshot,
    today: dict[str, Any],
    agent_run_id: int | None = None,
    notes: str = "",
) -> Settlement:
    """写入或更新当日清算记录（每交易日一条）。"""
    trade_date = date.fromisoformat(str(today["date"]))
    open_equity = float(view["day_start_equity"])
    close_equity = float(view["equity"])
    open_cash = _open_cash_baseline(db, trade_date, float(view["cash"]))
    close_cash = float(view["cash"])
    buy_amount, sell_amount = _day_trade_amounts(today.get("trades") or [])
    positions = {p["symbol"]: p for p in (view.get("positions") or [])}

    row = (
        db.query(Settlement)
        .filter(Settlement.trade_date == trade_date)
        .one_or_none()
    )
    if row is None:
        row = Settlement(trade_date=trade_date)
        db.add(row)

    row.open_equity = open_equity
    row.close_equity = close_equity
    row.open_cash = open_cash
    row.close_cash = close_cash
    row.day_pnl = float(view["day_pnl"])
    row.day_pnl_pct = float(view["day_pnl_pct"])
    row.total_pnl = float(view["total_pnl"])
    row.total_pnl_pct = float(view["total_pnl_pct"])
    row.unrealized_pnl = float(view["unrealized_pnl"])
    row.buy_amount = buy_amount
    row.sell_amount = sell_amount
    row.trade_count = int(today.get("trade_count") or len(today.get("trades") or []))
    row.order_count = int(today.get("order_count") or len(today.get("orders") or []))
    row.decision_count = int(
        today.get("decision_count") or len(today.get("decisions") or [])
    )
    row.positions = positions
    row.snapshot_id = snap.id
    if agent_run_id is not None:
        row.agent_run_id = agent_run_id
    if notes:
        row.notes = notes
    db.flush()
    return row


def list_settlements(db: Session, *, limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit), 365))
    rows = (
        db.query(Settlement)
        .order_by(Settlement.trade_date.desc())
        .limit(limit)
        .all()
    )
    return {
        "count": len(rows),
        "items": [_settlement_dict(r) for r in rows],
    }


def get_settlement(db: Session, trade_date: date | None = None) -> dict[str, Any] | None:
    day = trade_date or _today_cn()
    row = db.query(Settlement).filter(Settlement.trade_date == day).one_or_none()
    return _settlement_dict(row) if row else None


def settle_day(db: Session, *, agent_run_id: int | None = None) -> dict[str, Any]:
    """日终清算：按最新行情估值、写入快照与每日清算记录，并汇总当日盈亏/持仓/成交。"""
    from evo_api.services.market_session import require_review_window

    require_review_window()
    view = portfolio_view(db)
    snap = take_snapshot(db)
    today = desk_today(db)
    settlement = upsert_settlement(
        db,
        view=view,
        snap=snap,
        today=today,
        agent_run_id=agent_run_id,
        notes="日终清算",
    )
    return {
        "settled": True,
        "date": today["date"],
        "settlement_id": settlement.id,
        "snapshot_id": snap.id,
        "open_equity": settlement.open_equity,
        "close_equity": settlement.close_equity,
        "open_cash": settlement.open_cash,
        "close_cash": settlement.close_cash,
        "cash": view["cash"],
        "equity": view["equity"],
        "day_start_equity": view["day_start_equity"],
        "day_pnl": view["day_pnl"],
        "day_pnl_pct": view["day_pnl_pct"],
        "total_pnl": view["total_pnl"],
        "total_pnl_pct": view["total_pnl_pct"],
        "unrealized_pnl": view["unrealized_pnl"],
        "buy_amount": settlement.buy_amount,
        "sell_amount": settlement.sell_amount,
        "positions": view["positions"],
        "orders_today": today["orders"],
        "trades_today": today["trades"],
        "decisions_today": _slim_decisions(today["decisions"]),
        "decision_count": len(today["decisions"]),
        "order_count": len(today["orders"]),
        "trade_count": len(today["trades"]),
        "settlement": _settlement_dict(settlement),
    }


def day_report(db: Session) -> dict[str, Any]:
    """读取当日清算视角报告（不强制新快照；附最近快照与清算记录若存在）。"""
    view = portfolio_view(db)
    today = desk_today(db)
    snaps = (
        db.query(PortfolioSnapshot)
        .order_by(PortfolioSnapshot.id.desc())
        .limit(20)
        .all()
    )
    latest_today = None
    for s in snaps:
        if _to_cn_date(s.created_at) == _today_cn():
            latest_today = {
                "snapshot_id": s.id,
                "cash": s.cash,
                "equity": s.equity,
                "positions": s.positions,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            break
    settlement = get_settlement(db)
    return {
        "date": today["date"],
        "portfolio": {
            "cash": view["cash"],
            "equity": view["equity"],
            "day_pnl": view["day_pnl"],
            "day_pnl_pct": view["day_pnl_pct"],
            "total_pnl": view["total_pnl"],
            "total_pnl_pct": view["total_pnl_pct"],
            "unrealized_pnl": view["unrealized_pnl"],
            "positions": view["positions"],
        },
        "latest_snapshot_today": latest_today,
        "settlement": settlement,
        "decision_count": len(today["decisions"]),
        "order_count": len(today["orders"]),
        "trade_count": len(today["trades"]),
        "decisions": _slim_decisions(today["decisions"]),
        "orders": today["orders"],
        "trades": today["trades"],
    }


def list_today_decisions(db: Session) -> dict[str, Any]:
    today = desk_today(db)
    return {
        "date": today["date"],
        "items": _slim_decisions(today["decisions"]),
        "count": len(today["decisions"]),
    }


def list_today_orders(db: Session) -> dict[str, Any]:
    today = desk_today(db)
    return {
        "date": today["date"],
        "orders": today["orders"],
        "trades": today["trades"],
        "order_count": len(today["orders"]),
        "trade_count": len(today["trades"]),
    }


def submit_order(
    db: Session,
    *,
    symbol: str,
    side: str,
    quantity: float,
    decision_id: int | None = None,
    agent_run_id: int | None = None,
) -> dict[str, Any]:
    symbol = symbol.strip().upper().replace(".SH", "").replace(".SZ", "")
    digits = "".join(ch for ch in symbol if ch.isdigit())
    symbol = digits[-6:] if len(digits) >= 6 else symbol

    kind = instrument_kind(symbol)
    if kind is None:
        raise ValueError(f"{symbol} 不是可交易的 A 股个股或 ETF（指数不可下单）")
    rules = kind_rules(kind)
    lot = float(rules.get("lot_size", 100))
    if quantity <= 0:
        raise ValueError("数量必须为正数")
    if quantity % lot != 0:
        raise ValueError(f"数量必须是手数单位 {lot} 的整数倍")
    if side not in ("buy", "sell"):
        raise ValueError("买卖方向只能是 buy 或 sell")
    if side == "sell" and rules.get("allow_short") is False:
        pos = db.query(Position).filter_by(symbol=symbol).one_or_none()
        if not pos or pos.quantity < quantity:
            raise ValueError("持仓不足，且不允许做空")
        sellable = sellable_quantity(db, symbol, pos.quantity)
        if quantity > sellable + 1e-9:
            raise ValueError(
                f"T+1 限制：{symbol} 可卖 {sellable}，请求卖出 {quantity} "
                f"（当日买入锁定 {pos.quantity - sellable}）"
            )

    acct = ensure_account(db)
    if not acct.paper:
        raise ValueError("一期禁止实盘交易")

    price = get_price(symbol)
    notional = price * quantity
    commission_rate = float(rules.get("commission_rate", 0.0))
    min_commission = float(rules.get("min_commission", 0.0))
    commission = max(notional * commission_rate, min_commission)

    now = market_session_svc.now_cn()
    order = Order(
        decision_id=decision_id,
        agent_run_id=agent_run_id,
        symbol=symbol,
        side=OrderSide(side),
        quantity=quantity,
        status=OrderStatus.filled,
        created_at=now,
    )

    if side == "buy":
        cost = notional + commission
        if acct.cash < cost:
            order.status = OrderStatus.rejected
            order.reason = "现金不足"
            db.add(order)
            db.flush()
            return {"order_id": order.id, "status": order.status.value, "reason": order.reason}
        acct.cash -= cost
        pos = db.query(Position).filter_by(symbol=symbol).one_or_none()
        if not pos:
            pos = Position(symbol=symbol, quantity=0.0, avg_cost=0.0)
            db.add(pos)
            db.flush()
        new_qty = pos.quantity + quantity
        pos.avg_cost = (
            (pos.avg_cost * pos.quantity + notional) / new_qty if new_qty else 0.0
        )
        pos.quantity = new_qty
    else:
        proceeds = notional - commission
        pos = db.query(Position).filter_by(symbol=symbol).one()
        pos.quantity -= quantity
        acct.cash += proceeds
        if pos.quantity == 0:
            pos.avg_cost = 0.0

    db.add(order)
    db.flush()
    trade = Trade(
        order_id=order.id,
        symbol=symbol,
        side=OrderSide(side),
        quantity=quantity,
        price=price,
        commission=commission,
        created_at=now,
    )
    db.add(trade)
    snap = take_snapshot(db)
    db.flush()
    return {
        "order_id": order.id,
        "status": order.status.value,
        "trade_id": trade.id,
        "price": price,
        "commission": commission,
        "snapshot_id": snap.id,
        "portfolio": portfolio_view(db),
    }