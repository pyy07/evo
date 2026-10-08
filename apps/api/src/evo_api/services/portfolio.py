from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from evo_api.config import get_settings, load_instruments
from evo_api.models.entities import (
    Order,
    OrderSide,
    OrderStatus,
    PortfolioAccount,
    PortfolioSnapshot,
    Position,
    Trade,
)
from market_data.provider import MarketDataError, get_provider


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


def looks_like_etf(symbol: str) -> bool:
    digits = "".join(ch for ch in symbol if ch.isdigit())
    if len(digits) < 6:
        return False
    code = digits[-6:]
    prefixes = load_instruments().get("kinds", {}).get("etf", {}).get("code_prefixes", [])
    return any(code.startswith(p) for p in prefixes)


def etf_rules() -> dict[str, Any]:
    return load_instruments().get("kinds", {}).get("etf", {})


def get_price(symbol: str) -> float:
    provider = get_provider()
    try:
        quotes = provider.get_market_snapshot([symbol])
    except MarketDataError as exc:
        raise ValueError(f"market data unavailable: {exc}") from exc
    q = quotes.get(symbol) or next(iter(quotes.values()), None)
    if not q:
        raise ValueError(f"no quote for {symbol}")
    for key in ("price", "current", "last", "close"):
        if key in q and q[key] is not None:
            return float(q[key])
    raise ValueError(f"quote missing price for {symbol}: {q}")


def portfolio_view(db: Session) -> dict[str, Any]:
    acct = ensure_account(db)
    positions = db.query(Position).filter(Position.quantity != 0).all()
    marked: list[dict[str, Any]] = []
    equity = acct.cash
    for p in positions:
        try:
            px = get_price(p.symbol)
        except ValueError:
            px = p.avg_cost
        mv = px * p.quantity
        equity += mv
        marked.append(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_cost": p.avg_cost,
                "mark_price": px,
                "market_value": mv,
            }
        )
    return {
        "cash": acct.cash,
        "equity": equity,
        "currency": acct.currency,
        "paper": acct.paper,
        "positions": marked,
    }


def take_snapshot(db: Session) -> PortfolioSnapshot:
    view = portfolio_view(db)
    snap = PortfolioSnapshot(
        cash=view["cash"],
        equity=view["equity"],
        positions={p["symbol"]: p for p in view["positions"]},
    )
    db.add(snap)
    db.flush()
    return snap


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

    rules = etf_rules()
    lot = float(rules.get("lot_size", 100))
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if quantity % lot != 0:
        raise ValueError(f"quantity must be multiple of lot_size={lot}")
    if not looks_like_etf(symbol):
        raise ValueError(f"{symbol} does not look like an A-share ETF under kind rules")
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    if side == "sell" and rules.get("allow_short") is False:
        pos = db.query(Position).filter_by(symbol=symbol).one_or_none()
        if not pos or pos.quantity < quantity:
            raise ValueError("insufficient position; shorting disabled for ETF")

    acct = ensure_account(db)
    if not acct.paper:
        raise ValueError("live trading is disabled in phase 1")

    price = get_price(symbol)
    notional = price * quantity
    commission_rate = float(rules.get("commission_rate", 0.0))
    min_commission = float(rules.get("min_commission", 0.0))
    commission = max(notional * commission_rate, min_commission)

    order = Order(
        decision_id=decision_id,
        agent_run_id=agent_run_id,
        symbol=symbol,
        side=OrderSide(side),
        quantity=quantity,
        status=OrderStatus.filled,
    )

    if side == "buy":
        cost = notional + commission
        if acct.cash < cost:
            order.status = OrderStatus.rejected
            order.reason = "insufficient cash"
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