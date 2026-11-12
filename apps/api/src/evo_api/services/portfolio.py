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
    InvestmentDecision,
    Order,
    OrderSide,
    OrderStatus,
    PortfolioAccount,
    PortfolioSnapshot,
    Position,
    Review,
    Settlement,
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
    """盘中成交用：最新价（可能非收盘价）。"""
    return float(get_mark_quote(symbol, mode="last")["price"])


def get_mark_quote(
    symbol: str,
    *,
    mode: str = "last",
    as_of: date | None = None,
) -> dict[str, Any]:
    """估值报价。mode=close 优先官方/K 线收盘价；mode=last 用快照最新价。"""
    symbol = "".join(ch for ch in str(symbol) if ch.isdigit())[-6:] or str(symbol).strip()
    as_of = as_of or _today_cn()
    provider = get_provider()
    intraday: float | None = None
    try:
        quotes = provider.get_market_snapshot([symbol])
        q = quotes.get(symbol) or next(iter(quotes.values()), None) or {}
        for key in ("price", "current", "last", "close"):
            if key in q and q[key] is not None:
                try:
                    intraday = float(q[key])
                    break
                except (TypeError, ValueError):
                    pass
    except MarketDataError:
        q = {}
    if mode == "last":
        if intraday is None:
            raise ValueError(f"找不到报价：{symbol}")
        return {
            "price": intraday,
            "mark_price_intraday": intraday,
            "mark_price_source": "last",
            "mark_as_of": as_of.isoformat(),
        }
    # close：K 线收盘（≤ as_of 最近一根）
    try:
        bars = provider.get_etf_history(symbol, count=15) or []
    except Exception:  # noqa: BLE001
        bars = []
    close_px: float | None = None
    close_date: str | None = None
    for bar in reversed(list(bars)):
        raw = str(bar.get("date") or "")[:10]
        try:
            bd = date.fromisoformat(raw)
        except ValueError:
            continue
        if bd > as_of:
            continue
        px = bar.get("close") if bar.get("close") is not None else bar.get("price")
        try:
            close_px = float(px)
            close_date = bd.isoformat()
            break
        except (TypeError, ValueError):
            continue
    if close_px is not None:
        out = {
            "price": close_px,
            "mark_price_source": "close",
            "mark_as_of": close_date or as_of.isoformat(),
        }
        if intraday is not None:
            out["mark_price_intraday"] = intraday
        return out
    if intraday is not None:
        return {
            "price": intraday,
            "mark_price_intraday": intraday,
            "mark_price_source": "last_fallback",
            "mark_as_of": as_of.isoformat(),
            "mark_tip": "收盘价不可用，已回退最新价",
        }
    raise ValueError(f"找不到报价：{symbol}")


def previous_trading_day(day: date) -> date:
    d = day - timedelta(days=1)
    for _ in range(20):
        try:
            if market_session_svc.is_trading_day(d):
                return d
        except Exception:  # noqa: BLE001
            if d.weekday() < 5:
                return d
        d -= timedelta(days=1)
    return day - timedelta(days=1)


def trading_days_between(start_exclusive: date, end_exclusive: date) -> list[date]:
    """(start, end) 开区间内的交易日。"""
    out: list[date] = []
    d = start_exclusive + timedelta(days=1)
    guard = 0
    while d < end_exclusive and guard < 40:
        guard += 1
        try:
            ok = market_session_svc.is_trading_day(d)
        except Exception:  # noqa: BLE001
            ok = d.weekday() < 5
        if ok:
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed_name_map() -> dict[str, str]:
    out: dict[str, str] = {}
    kinds = load_instruments().get("kinds") or {}
    for kind in kinds.values():
        if not isinstance(kind, dict):
            continue
        universe = kind.get("universe") or {}
        for item in universe.get("seed_codes") or []:
            if isinstance(item, dict) and item.get("code"):
                code = "".join(ch for ch in str(item["code"]) if ch.isdigit())[-6:]
                name = str(item.get("name") or "").strip()
                if code and name:
                    out[code] = name
    return out


def resolve_instrument_names(codes: list[str]) -> dict[str, str]:
    """代码→名称：优先种子池，其次行情快照 name。"""
    bare: list[str] = []
    for c in codes:
        d = "".join(ch for ch in str(c) if ch.isdigit())[-6:]
        if d:
            bare.append(d)
    bare = list(dict.fromkeys(bare))
    names = dict(_seed_name_map())
    need = [c for c in bare if c not in names]
    if need:
        try:
            quotes = get_provider().get_market_snapshot(need)
        except Exception:  # noqa: BLE001
            quotes = {}
        for code in need:
            q = quotes.get(code) or {}
            name = str(q.get("name") or q.get("名称") or "").strip()
            if name:
                names[code] = name
    return {c: names.get(c, c) for c in bare}


def _mark_positions(
    positions: list[Position] | list[dict[str, Any]],
    *,
    mark_mode: str,
    as_of: date,
    name_map: dict[str, str],
    db: Session | None = None,
) -> tuple[list[dict[str, Any]], float, float, float]:
    """返回 (marked, equity_ex_cash_add, unrealized, cost_basis)；调用方再加 cash。"""
    marked: list[dict[str, Any]] = []
    mv_sum = 0.0
    unrealized = 0.0
    cost_basis = 0.0
    for p in positions:
        if isinstance(p, dict):
            symbol = str(p.get("symbol") or "")
            qty = float(p.get("quantity") or 0)
            avg_cost = float(p.get("avg_cost") or 0)
        else:
            symbol = p.symbol
            qty = float(p.quantity)
            avg_cost = float(p.avg_cost)
        if abs(qty) < 1e-12:
            continue
        try:
            quote = get_mark_quote(symbol, mode=mark_mode, as_of=as_of)
            px = float(quote["price"])
            src = quote.get("mark_price_source")
            intraday = quote.get("mark_price_intraday")
            mark_as_of = quote.get("mark_as_of")
        except ValueError:
            px = avg_cost
            src = "avg_cost_fallback"
            intraday = None
            mark_as_of = as_of.isoformat()
        mv = px * qty
        cost = avg_cost * qty
        pnl = mv - cost
        mv_sum += mv
        unrealized += pnl
        cost_basis += cost
        code = "".join(ch for ch in str(symbol) if ch.isdigit())[-6:] or symbol
        row: dict[str, Any] = {
            "symbol": symbol,
            "name": name_map.get(code) or symbol,
            "quantity": qty,
            "avg_cost": avg_cost,
            "mark_price": px,
            "mark_price_source": src,
            "mark_as_of": mark_as_of,
            "market_value": mv,
            "cost_basis": cost,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": (pnl / cost * 100.0) if cost else 0.0,
        }
        if intraday is not None:
            row["mark_price_intraday"] = intraday
        if db is not None and not isinstance(p, dict):
            row["sellable_quantity"] = sellable_quantity(db, symbol, qty)
            row["locked_quantity"] = bought_today_quantity(db, symbol)
        marked.append(row)
    return marked, mv_sum, unrealized, cost_basis


def _open_basis(db: Session, trade_date: date, *, initial_cash: float) -> dict[str, Any]:
    """日初权益/现金基准：优先上一交易日清算；缺失则标 stale。"""
    prev_td = previous_trading_day(trade_date)
    exact = (
        db.query(Settlement)
        .filter(Settlement.trade_date == prev_td)
        .one_or_none()
    )
    if exact is not None:
        return {
            "open_equity": float(exact.close_equity),
            "open_cash": float(exact.close_cash),
            "open_basis_date": prev_td,
            "open_basis_stale": False,
            "prev_close_source": "settlement",
            "prev_positions": exact.positions or {},
        }
    any_prior = (
        db.query(Settlement)
        .filter(Settlement.trade_date < trade_date)
        .order_by(Settlement.trade_date.desc())
        .first()
    )
    snap = _latest_snapshot_on_or_before(db, prev_td)
    if snap is not None:
        name_map = resolve_instrument_names(
            [str(k) for k in (snap.positions or {}).keys()]
        )
        norm: list[dict[str, Any]] = []
        for sym, v in (snap.positions or {}).items():
            if not isinstance(v, dict):
                continue
            norm.append(
                {
                    "symbol": str(sym),
                    "quantity": float(v.get("quantity") or 0),
                    "avg_cost": float(v.get("avg_cost") or 0),
                }
            )
        marked, mv, _, _ = _mark_positions(
            norm, mark_mode="close", as_of=prev_td, name_map=name_map
        )
        equity = float(snap.cash) + mv
        return {
            "open_equity": equity,
            "open_cash": float(snap.cash),
            "open_basis_date": prev_td,
            "open_basis_stale": any_prior is None
            or any_prior.trade_date != prev_td,
            "prev_close_source": "snapshot_revalued",
            "prev_positions": {p["symbol"]: p for p in marked},
            "open_basis_tip": (
                f"上一交易日 {prev_td.isoformat()} 无清算行，"
                f"已用快照#{snap.id} 按收盘价重估"
                + (
                    f"（最近清算日={any_prior.trade_date.isoformat()}）"
                    if any_prior
                    else ""
                )
            ),
        }
    if any_prior is not None:
        return {
            "open_equity": float(any_prior.close_equity),
            "open_cash": float(any_prior.close_cash),
            "open_basis_date": any_prior.trade_date,
            "open_basis_stale": True,
            "prev_close_source": "settlement_gap",
            "prev_positions": any_prior.positions or {},
            "open_basis_tip": (
                f"缺 {prev_td.isoformat()} 清算，暂用 {any_prior.trade_date.isoformat()} "
                "收盘权益；day_pnl 可能跨多日，勿当单日归因"
            ),
        }
    return {
        "open_equity": float(initial_cash),
        "open_cash": float(initial_cash),
        "open_basis_date": None,
        "open_basis_stale": False,
        "prev_close_source": "initial_cash",
        "prev_positions": {},
    }


def _latest_snapshot_on_or_before(db: Session, day: date) -> PortfolioSnapshot | None:
    snaps = (
        db.query(PortfolioSnapshot)
        .order_by(PortfolioSnapshot.created_at.desc())
        .limit(800)
        .all()
    )
    on_day: PortfolioSnapshot | None = None
    before: PortfolioSnapshot | None = None
    for s in snaps:
        d = _to_cn_date(s.created_at)
        if not d:
            continue
        if d == day and on_day is None:
            on_day = s
        if d < day and before is None:
            before = s
        if on_day and before:
            break
    return on_day or before


def portfolio_view(
    db: Session,
    *,
    mark_mode: str = "last",
    as_of: date | None = None,
) -> dict[str, Any]:
    acct = ensure_account(db)
    settings = get_settings()
    instruments = load_instruments()
    initial_cash = float(instruments.get("default_cash", settings.initial_cash))
    positions = db.query(Position).filter(Position.quantity != 0).all()
    name_map = resolve_instrument_names([p.symbol for p in positions])
    today = as_of or _today_cn()
    marked, mv_sum, unrealized, cost_basis = _mark_positions(
        positions,
        mark_mode=mark_mode,
        as_of=today,
        name_map=name_map,
        db=db,
    )
    equity = float(acct.cash) + mv_sum
    total_pnl = equity - initial_cash
    basis = _open_basis(db, today, initial_cash=initial_cash)
    day_start_equity = float(basis["open_equity"])
    day_pnl = equity - day_start_equity
    prev_pos = basis.get("prev_positions") or {}
    # 当日成交：用于持仓级 day_pnl（买入腿按现价−成交价）
    today_trades = [
        t
        for t in db.query(Trade).order_by(Trade.id.asc()).all()
        if _to_cn_date(t.created_at) == today
    ]
    buy_qty: dict[str, float] = {}
    buy_notional: dict[str, float] = {}
    sell_qty: dict[str, float] = {}
    sell_notional: dict[str, float] = {}
    for t in today_trades:
        sym = t.symbol
        qty = float(t.quantity)
        px = float(t.price)
        if t.side == OrderSide.buy or str(t.side) == "buy":
            buy_qty[sym] = buy_qty.get(sym, 0.0) + qty
            buy_notional[sym] = buy_notional.get(sym, 0.0) + px * qty
        else:
            sell_qty[sym] = sell_qty.get(sym, 0.0) + qty
            sell_notional[sym] = sell_notional.get(sym, 0.0) + px * qty
    for row in marked:
        sym = row["symbol"]
        prev = prev_pos.get(sym) or {}
        prev_qty = float(prev.get("quantity") or 0)
        prev_px = float(prev.get("mark_price") or prev.get("avg_cost") or 0)
        curr_qty = float(row["quantity"])
        curr_px = float(row["mark_price"])
        b_qty = float(buy_qty.get(sym) or 0)
        s_qty = float(sell_qty.get(sym) or 0)
        buy_avg = (buy_notional[sym] / b_qty) if b_qty > 1e-12 else 0.0
        sell_avg = (sell_notional[sym] / s_qty) if s_qty > 1e-12 else 0.0
        retained = max(0.0, prev_qty - s_qty)
        hold = retained * (curr_px - prev_px) if retained and prev_px else 0.0
        sell_leg = s_qty * (sell_avg - prev_px) if s_qty and prev_px else 0.0
        buy_leg = b_qty * (curr_px - buy_avg) if b_qty else 0.0
        pos_day = hold + sell_leg + buy_leg
        row["prev_close"] = prev_px or None
        row["prev_qty"] = prev_qty
        row["day_pnl"] = round(pos_day, 2)
        # 涨跌幅：相对昨收（有昨收时）；新开仓相对买入均价
        if prev_px:
            row["day_pnl_pct"] = round((curr_px - prev_px) / prev_px * 100.0, 3)
        elif buy_avg:
            row["day_pnl_pct"] = round((curr_px - buy_avg) / buy_avg * 100.0, 3)
        else:
            row["day_pnl_pct"] = None
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
        "open_cash": basis["open_cash"],
        "open_basis_date": (
            basis["open_basis_date"].isoformat()
            if isinstance(basis.get("open_basis_date"), date)
            else basis.get("open_basis_date")
        ),
        "open_basis_stale": bool(basis.get("open_basis_stale")),
        "prev_close_source": basis.get("prev_close_source"),
        "open_basis_tip": basis.get("open_basis_tip"),
        "mark_mode": mark_mode,
        "positions": marked,
        "_prev_positions": prev_pos,
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
                "get_hsgt_flow",
                "get_etf_premium",
                "get_national_team_flow",
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
    name_map = resolve_instrument_names(
        [o.symbol for o in orders] + [t.symbol for t in trades]
    )
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
                "name": name_map.get(
                    "".join(ch for ch in str(o.symbol) if ch.isdigit())[-6:] or o.symbol,
                    o.symbol,
                ),
                "side": o.side.value,
                "quantity": o.quantity,
                "status": o.status.value,
                "reason": o.reason,
                "rationale": o.rationale,
                "confidence": o.confidence,
                "verify_spec": o.verify_spec,
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
        "trades": [_trade_item(t, name_map=name_map) for t in trades],
    }


def _trade_item(t: Trade, *, name_map: dict[str, str] | None = None) -> dict[str, Any]:
    code = "".join(ch for ch in str(t.symbol) if ch.isdigit())[-6:] or t.symbol
    name = (name_map or {}).get(code) or t.symbol
    today = _today_cn()
    due = t.verify_due_date
    st = (
        t.verify_status.value
        if hasattr(t.verify_status, "value")
        else t.verify_status
    )
    return {
        "id": t.id,
        "order_id": t.order_id,
        "symbol": t.symbol,
        "name": name,
        "side": t.side.value,
        "quantity": t.quantity,
        "price": t.price,
        "amount": round(float(t.price) * float(t.quantity), 2),
        "commission": t.commission,
        "rationale": t.rationale,
        "confidence": t.confidence,
        "verify_spec": t.verify_spec,
        "verify_due_date": due.isoformat() if due else None,
        "verify_status": st,
        "is_due": bool(st == "pending" and due is not None and due <= today),
        "verify_eval": t.verify_eval,
        "verify_outcome": t.verify_outcome,
        "verify_lesson": t.verify_lesson,
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
    picked: list[Trade] = []
    for t in rows:
        d = _to_cn_date(t.created_at)
        if exclude_today and d == today:
            continue
        picked.append(t)
        if len(picked) >= limit:
            break
    name_map = resolve_instrument_names([t.symbol for t in picked])
    items = [_trade_item(t, name_map=name_map) for t in picked]
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
        "open_basis_date": (
            row.open_basis_date.isoformat() if row.open_basis_date else None
        ),
        "open_basis_stale": bool(row.open_basis_stale),
        "attribution": row.attribution,
        "notes": row.notes or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _build_attribution(
    *,
    trade_date: date,
    view: dict[str, Any],
    trades: list[dict[str, Any]],
    buy_amount: float,
    sell_amount: float,
) -> dict[str, Any]:
    """持仓级日归因。

    留存仓：retained = max(0, prev_qty − sold_qty)，计入 holding_price_pnl；
    卖出腿：(卖出价 − 昨收) × 卖出量；买入腿：(收盘 − 买入价) × 买入量；
    费用单列。恒等式：equity_delta ≈ holding + trade_mark − fees。
    """
    prev_pos = view.get("_prev_positions") or {}
    curr_pos = {p["symbol"]: p for p in (view.get("positions") or [])}

    # 按标的汇总当日买卖
    buy_qty: dict[str, float] = {}
    sell_qty: dict[str, float] = {}
    buy_notional: dict[str, float] = {}
    sell_notional: dict[str, float] = {}
    fees = 0.0
    for t in trades:
        sym = str(t.get("symbol") or "")
        qty = float(t.get("quantity") or 0)
        px = float(t.get("price") or 0)
        fee = float(t.get("commission") or 0)
        fees += fee
        side = t.get("side")
        if side == "buy" or side == OrderSide.buy:
            buy_qty[sym] = buy_qty.get(sym, 0.0) + qty
            buy_notional[sym] = buy_notional.get(sym, 0.0) + px * qty
        elif side == "sell" or side == OrderSide.sell:
            sell_qty[sym] = sell_qty.get(sym, 0.0) + qty
            sell_notional[sym] = sell_notional.get(sym, 0.0) + px * qty

    symbols = sorted(set(prev_pos) | set(curr_pos) | set(buy_qty) | set(sell_qty))
    contrib: list[dict[str, Any]] = []
    holding_pnl = 0.0
    trade_mark_pnl = 0.0
    for sym in symbols:
        prev = prev_pos.get(sym) or {}
        curr = curr_pos.get(sym) or {}
        prev_qty = float(prev.get("quantity") or 0)
        curr_qty = float(curr.get("quantity") or 0)
        prev_px = float(prev.get("mark_price") or prev.get("avg_cost") or 0)
        curr_px = float(curr.get("mark_price") or curr.get("avg_cost") or 0)
        b_qty = float(buy_qty.get(sym) or 0)
        s_qty = float(sell_qty.get(sym) or 0)
        buy_avg = (buy_notional[sym] / b_qty) if b_qty > 1e-12 else 0.0
        sell_avg = (sell_notional[sym] / s_qty) if s_qty > 1e-12 else 0.0
        # 无当日收盘时，买入腿用成交价占位（pnl=0）；卖出腿用昨收
        close_px = curr_px or sell_avg or buy_avg or prev_px
        prev_close = prev_px or sell_avg or buy_avg or close_px

        retained = max(0.0, prev_qty - s_qty)
        hold_pnl = retained * (close_px - prev_close) if retained and prev_close else 0.0
        sell_leg = s_qty * (sell_avg - prev_close) if s_qty and prev_close else 0.0
        buy_leg = b_qty * (close_px - buy_avg) if b_qty else 0.0
        mark_pnl = sell_leg + buy_leg
        holding_pnl += hold_pnl
        trade_mark_pnl += mark_pnl

        implied_curr = prev_qty + b_qty - s_qty
        if abs(prev_qty) > 1e-9 or abs(curr_qty) > 1e-9 or b_qty or s_qty:
            contrib.append(
                {
                    "symbol": sym,
                    "prev_qty": prev_qty,
                    "curr_qty": curr_qty,
                    "buy_qty": b_qty,
                    "sell_qty": s_qty,
                    "traded_qty": b_qty + s_qty,
                    "retained_qty": retained,
                    "prev_close": prev_close or None,
                    "close": close_px or None,
                    "buy_avg": buy_avg or None,
                    "sell_avg": sell_avg or None,
                    "holding_price_pnl": round(hold_pnl, 2),
                    "trade_mark_pnl": round(mark_pnl, 2),
                    "sell_leg_pnl": round(sell_leg, 2),
                    "buy_leg_pnl": round(buy_leg, 2),
                    "qty_reconcile_delta": round(curr_qty - implied_curr, 4),
                }
            )

    equity_delta = float(view["day_pnl"])
    warnings: list[str] = []
    if view.get("open_basis_stale"):
        warnings.append(
            view.get("open_basis_tip")
            or "open_basis_stale：日初基准非上一交易日清算，day_pnl 可能跨日"
        )
    # 持仓数量对账
    for row in contrib:
        if abs(float(row.get("qty_reconcile_delta") or 0)) > 1e-6:
            warnings.append(
                f"{row['symbol']} 持仓变动不对账："
                f"prev {row['prev_qty']}+buy {row['buy_qty']}-sell {row['sell_qty']}"
                f" ≠ curr {row['curr_qty']}（Δ={row['qty_reconcile_delta']}）"
            )
    # 归因恒等式
    attributed = holding_pnl + trade_mark_pnl - fees
    gap = attributed - equity_delta
    if abs(gap) >= 1.0:
        warnings.append(
            f"归因不对账：holding {holding_pnl:.2f}+trade_mark {trade_mark_pnl:.2f}"
            f"-fees {fees:.2f} = {attributed:.2f}，equity_delta={equity_delta:.2f}，"
            f"差额 {gap:.2f}"
        )
    # 现金对账（买入付佣金、卖出扣佣金）
    cash_delta = float(view["cash"]) - float(view.get("open_cash") or view["cash"])
    expected_cash_delta = sell_amount - buy_amount - fees
    if abs(cash_delta - expected_cash_delta) > max(1.0, 0.002 * max(buy_amount, sell_amount, 1)):
        warnings.append(
            f"现金变动 {cash_delta:.2f} 与买卖额−佣金 {expected_cash_delta:.2f} 不一致"
            f"（跨日基准/补清算时属预期）"
        )
    basis_date = view.get("open_basis_date")
    if isinstance(basis_date, date):
        basis_date = basis_date.isoformat()
    elif basis_date is not None:
        basis_date = str(basis_date)[:10]
    return {
        "method": "retained_holding+trade_legs-fees",
        "attribution_window": {
            "start": basis_date,
            "end": trade_date.isoformat(),
        },
        "prev_close_source": view.get("prev_close_source"),
        "open_basis_stale": bool(view.get("open_basis_stale")),
        "equity_delta": round(equity_delta, 2),
        "holding_price_pnl": round(holding_pnl, 2),
        "trade_mark_pnl": round(trade_mark_pnl, 2),
        "fees": round(fees, 2),
        "attributed_pnl": round(attributed, 2),
        "reconcile_gap": round(gap, 2),
        "positions": contrib,
        "warnings": warnings,
        "mark_mode": view.get("mark_mode"),
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
    open_cash = float(view.get("open_cash") if view.get("open_cash") is not None else view["cash"])
    close_cash = float(view["cash"])
    buy_amount, sell_amount = _day_trade_amounts(today.get("trades") or [])
    positions = {p["symbol"]: p for p in (view.get("positions") or [])}
    attribution = _build_attribution(
        trade_date=trade_date,
        view=view,
        trades=today.get("trades") or [],
        buy_amount=buy_amount,
        sell_amount=sell_amount,
    )

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
    basis_raw = view.get("open_basis_date")
    if isinstance(basis_raw, date):
        row.open_basis_date = basis_raw
    elif basis_raw:
        try:
            row.open_basis_date = date.fromisoformat(str(basis_raw)[:10])
        except ValueError:
            row.open_basis_date = previous_trading_day(trade_date)
    else:
        row.open_basis_date = previous_trading_day(trade_date)
    row.open_basis_stale = bool(view.get("open_basis_stale"))
    row.attribution = attribution
    if agent_run_id is not None:
        row.agent_run_id = agent_run_id
    note_bits = [notes] if notes else []
    if view.get("open_basis_tip"):
        note_bits.append(str(view["open_basis_tip"]))
    for w in attribution.get("warnings") or []:
        note_bits.append(f"warning: {w}")
    if note_bits:
        row.notes = " | ".join(note_bits)
    db.flush()
    return row


def _backfill_settlement_day(db: Session, day: date) -> Settlement | None:
    """为缺失交易日补一行清算：优先当日快照按收盘价重估。"""
    existing = db.query(Settlement).filter(Settlement.trade_date == day).one_or_none()
    if existing is not None:
        return existing
    settings = get_settings()
    instruments = load_instruments()
    initial_cash = float(instruments.get("default_cash", settings.initial_cash))
    basis = _open_basis(db, day, initial_cash=initial_cash)
    snap = _latest_snapshot_on_or_before(db, day)
    if snap is None:
        return None
    name_map = resolve_instrument_names([str(k) for k in (snap.positions or {}).keys()])
    norm: list[dict[str, Any]] = []
    for sym, v in (snap.positions or {}).items():
        if not isinstance(v, dict):
            continue
        norm.append(
            {
                "symbol": str(sym),
                "quantity": float(v.get("quantity") or 0),
                "avg_cost": float(v.get("avg_cost") or 0),
            }
        )
    marked, mv, unrealized, cost_basis = _mark_positions(
        norm, mark_mode="close", as_of=day, name_map=name_map
    )
    cash = float(snap.cash)
    equity = cash + mv
    open_equity = float(basis["open_equity"])
    day_pnl = equity - open_equity
    # 当日成交
    trades = [
        _trade_item(t)
        for t in db.query(Trade).order_by(Trade.id.asc()).all()
        if _to_cn_date(t.created_at) == day
    ]
    buy_amount, sell_amount = _day_trade_amounts(trades)
    view = {
        "cash": cash,
        "equity": equity,
        "day_start_equity": open_equity,
        "open_cash": basis["open_cash"],
        "day_pnl": day_pnl,
        "day_pnl_pct": (day_pnl / open_equity * 100.0) if open_equity else 0.0,
        "total_pnl": equity - initial_cash,
        "total_pnl_pct": ((equity - initial_cash) / initial_cash * 100.0)
        if initial_cash
        else 0.0,
        "unrealized_pnl": unrealized,
        "positions": marked,
        "open_basis_date": basis.get("open_basis_date"),
        "open_basis_stale": basis.get("open_basis_stale"),
        "prev_close_source": basis.get("prev_close_source"),
        "open_basis_tip": basis.get("open_basis_tip"),
        "mark_mode": "close",
        "_prev_positions": basis.get("prev_positions") or {},
    }
    today_stub = {
        "date": day.isoformat(),
        "trades": trades,
        "orders": [],
        "decisions": [],
        "trade_count": len(trades),
        "order_count": 0,
        "decision_count": 0,
    }
    return upsert_settlement(
        db,
        view=view,
        snap=snap,
        today=today_stub,
        notes=f"补清算 {day.isoformat()}（缺日回填）",
    )


def ensure_prior_settlements(db: Session, trade_date: date) -> list[str]:
    """补齐首条清算日至 trade_date 之间所有缺失交易日（含历史空洞，如 10-19→10-21 缺 10-20）。"""
    first = (
        db.query(Settlement)
        .filter(Settlement.trade_date < trade_date)
        .order_by(Settlement.trade_date.asc())
        .first()
    )
    if first is None:
        return []
    existing = {
        r.trade_date
        for r in db.query(Settlement.trade_date)
        .filter(Settlement.trade_date < trade_date)
        .all()
    }
    missing = [
        d
        for d in trading_days_between(first.trade_date, trade_date)
        if d not in existing
    ]
    filled: list[str] = []
    for d in missing:
        row = _backfill_settlement_day(db, d)
        if row is not None:
            filled.append(d.isoformat())
            existing.add(d)
    # 回填后刷新「缺 open_basis / attribution」的历史行（如 10-21）
    refreshed = refresh_settlement_open_basis(db, until=trade_date)
    return filled + [f"refreshed:{x}" for x in refreshed]


def refresh_settlement_open_basis(
    db: Session, *, until: date | None = None
) -> list[str]:
    """对 open_basis_date/attribution 缺失的清算行按当前前序清算重算日初基准与归因。"""
    q = db.query(Settlement).order_by(Settlement.trade_date.asc())
    if until is not None:
        q = q.filter(Settlement.trade_date < until)
    rows = q.all()
    settings = get_settings()
    instruments = load_instruments()
    initial_cash = float(instruments.get("default_cash", settings.initial_cash))
    touched: list[str] = []
    for row in rows:
        prev_td = previous_trading_day(row.trade_date)
        prev_row = (
            db.query(Settlement)
            .filter(Settlement.trade_date == prev_td)
            .one_or_none()
        )
        needs = row.open_basis_date is None or row.attribution is None
        # 历史空洞补齐后：旧行仍指向更早日，需改挂到上一交易日
        if prev_row is not None and row.open_basis_date != prev_td:
            needs = True
        if not needs:
            continue
        basis = _open_basis(db, row.trade_date, initial_cash=initial_cash)
        open_equity = float(basis["open_equity"])
        open_cash = float(basis["open_cash"])
        close_equity = float(row.close_equity)
        day_pnl = close_equity - open_equity
        # 重建 attribution（用落库 positions 作当日收盘仓）
        pos_map = row.positions or {}
        marked = []
        for sym, v in pos_map.items():
            if isinstance(v, dict):
                marked.append(v if "symbol" in v else {"symbol": sym, **v})
        trades = [
            _trade_item(t)
            for t in db.query(Trade).order_by(Trade.id.asc()).all()
            if _to_cn_date(t.created_at) == row.trade_date
        ]
        buy_amount, sell_amount = _day_trade_amounts(trades)
        view = {
            "cash": float(row.close_cash),
            "equity": close_equity,
            "day_start_equity": open_equity,
            "open_cash": open_cash,
            "day_pnl": day_pnl,
            "day_pnl_pct": (day_pnl / open_equity * 100.0) if open_equity else 0.0,
            "positions": marked,
            "open_basis_date": basis.get("open_basis_date"),
            "open_basis_stale": basis.get("open_basis_stale"),
            "prev_close_source": basis.get("prev_close_source"),
            "open_basis_tip": basis.get("open_basis_tip"),
            "mark_mode": "close",
            "_prev_positions": basis.get("prev_positions") or {},
        }
        attr = _build_attribution(
            trade_date=row.trade_date,
            view=view,
            trades=trades,
            buy_amount=buy_amount,
            sell_amount=sell_amount,
        )
        row.open_equity = open_equity
        row.open_cash = open_cash
        row.day_pnl = day_pnl
        row.day_pnl_pct = float(view["day_pnl_pct"])
        basis_raw = basis.get("open_basis_date")
        if isinstance(basis_raw, date):
            row.open_basis_date = basis_raw
        elif basis_raw:
            try:
                row.open_basis_date = date.fromisoformat(str(basis_raw)[:10])
            except ValueError:
                row.open_basis_date = prev_td
        else:
            row.open_basis_date = None
        row.open_basis_stale = bool(basis.get("open_basis_stale"))
        row.attribution = attr
        tip = basis.get("open_basis_tip")
        note_bits = [row.notes] if row.notes else []
        if tip and tip not in (row.notes or ""):
            note_bits.append(str(tip))
        note_bits.append("历史 open_basis/attribution 已重算")
        row.notes = " | ".join(x for x in note_bits if x)
        touched.append(row.trade_date.isoformat())
    if touched:
        db.flush()
    return touched


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
    """日终清算：收盘价估值、补齐缺日清算、写入当日清算与归因。"""
    from evo_api.services.market_session import require_review_window

    require_review_window()
    today = desk_today(db)
    trade_date = date.fromisoformat(str(today["date"]))
    backfilled = ensure_prior_settlements(db, trade_date)
    # 补日后重新取日初基准，再用收盘价估值当日
    view = portfolio_view(db, mark_mode="close", as_of=trade_date)
    snap = take_snapshot(db)
    settlement = upsert_settlement(
        db,
        view=view,
        snap=snap,
        today=today,
        agent_run_id=agent_run_id,
        notes="日终清算（收盘价）",
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
        "open_basis_date": settlement.open_basis_date.isoformat()
        if settlement.open_basis_date
        else None,
        "open_basis_stale": bool(settlement.open_basis_stale),
        "attribution": settlement.attribution,
        "backfilled_dates": backfilled,
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
    rationale: str | None = None,
    confidence: float | None = None,
    verify: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from evo_api.services.trade_verify import add_trading_days, parse_order_evidence

    symbol = symbol.strip().upper().replace(".SH", "").replace(".SZ", "")
    digits = "".join(ch for ch in symbol if ch.isdigit())
    symbol = digits[-6:] if len(digits) >= 6 else symbol

    evidence = parse_order_evidence(
        rationale=rationale, confidence=confidence, verify=verify
    )

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
    trade_day = now.date()
    due_date = add_trading_days(
        trade_day, int(evidence["verify_spec"]["horizon_days"])
    )
    order = Order(
        decision_id=decision_id,
        agent_run_id=agent_run_id,
        symbol=symbol,
        side=OrderSide(side),
        quantity=quantity,
        status=OrderStatus.filled,
        rationale=evidence["rationale"],
        confidence=evidence["confidence"],
        verify_spec=evidence["verify_spec"],
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
        rationale=evidence["rationale"],
        confidence=evidence["confidence"],
        verify_spec=evidence["verify_spec"],
        verify_due_date=due_date,
        verify_status="pending",
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
        "rationale": trade.rationale,
        "confidence": trade.confidence,
        "verify_spec": trade.verify_spec,
        "verify_due_date": due_date.isoformat(),
        "verify_status": trade.verify_status,
        "snapshot_id": snap.id,
        "portfolio": portfolio_view(db),
    }


def _decision_payload(
    db: Session,
    d: InvestmentDecision,
    *,
    with_invocations: bool = False,
    with_orders: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": d.id,
        "summary": d.summary,
        "hypothesis": d.hypothesis,
        "action_plan": d.action_plan,
        "usage_notes": d.usage_notes or [],
        "agent_run_id": d.agent_run_id,
        "observation_id": d.observation_id,
        "thesis_id": d.thesis_id,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "trade_date": _to_cn_date(d.created_at).isoformat() if d.created_at else None,
    }
    if with_invocations:
        invocations: list[dict[str, Any]] = []
        if d.agent_run_id:
            run = db.get(AgentRun, d.agent_run_id)
            if run:
                invocations = invocations_for_run(db, run)
        payload["invocations"] = invocations
        payload["invocation_count"] = len(invocations)
    if with_orders:
        orders = (
            db.query(Order)
            .filter(Order.decision_id == d.id)
            .order_by(Order.id.asc())
            .all()
        )
        trades = (
            db.query(Trade)
            .filter(Trade.order_id.in_([o.id for o in orders] or [-1]))
            .all()
        )
        trade_by_order = {t.order_id: t for t in trades}
        payload["orders"] = [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side.value,
                "quantity": o.quantity,
                "status": o.status.value,
                "reason": o.reason,
                "rationale": o.rationale,
                "confidence": o.confidence,
                "verify_spec": o.verify_spec,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "price": trade_by_order[o.id].price if o.id in trade_by_order else None,
                "commission": (
                    trade_by_order[o.id].commission if o.id in trade_by_order else None
                ),
            }
            for o in orders
        ]
    return payload


def list_decisions(db: Session, *, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 300))
    rows = (
        db.query(InvestmentDecision)
        .order_by(InvestmentDecision.id.desc())
        .limit(limit)
        .all()
    )
    items = []
    for d in rows:
        item = _decision_payload(db, d, with_invocations=False, with_orders=False)
        notes = item["usage_notes"] or []
        item["usage_note_count"] = len(notes)
        item["preview"] = (d.summary or "")[:160]
        items.append(item)
    return {"items": items, "count": len(items)}


def get_decision(db: Session, decision_id: int) -> dict[str, Any] | None:
    d = db.get(InvestmentDecision, decision_id)
    if not d:
        return None
    return _decision_payload(db, d, with_invocations=True, with_orders=True)


def _review_payload(r: Review) -> dict[str, Any]:
    content = r.content or ""
    return {
        "id": r.id,
        "issue_type": r.issue_type.value,
        "content": content,
        "preview": content[:200].replace("\n", " ").strip(),
        "decision_id": r.decision_id,
        "agent_run_id": r.agent_run_id,
        "change_request_id": r.change_request_id,
        "create_change_request": bool(r.create_change_request),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "trade_date": _to_cn_date(r.created_at).isoformat() if r.created_at else None,
    }


def list_reviews(db: Session, *, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 300))
    rows = db.query(Review).order_by(Review.id.desc()).limit(limit).all()
    items = [_review_payload(r) for r in rows]
    return {"items": items, "count": len(items)}


def get_review(db: Session, review_id: int) -> dict[str, Any] | None:
    r = db.get(Review, review_id)
    if not r:
        return None
    return _review_payload(r)