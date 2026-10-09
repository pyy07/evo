"""A-share continuous-auction session (Asia/Shanghai)."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from market_data.provider import get_provider

CN_TZ = ZoneInfo("Asia/Shanghai")
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)
POSTCLOSE_TRIGGER = time(15, 5)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _row_is_open(row: dict[str, Any]) -> bool:
    if "is_open" in row and row["is_open"] is not None:
        return bool(row["is_open"])
    return bool(row.get("is_trading_day"))


def is_trading_day(day: date, calendar_days: list[dict[str, Any]] | None = None) -> bool:
    days = calendar_days
    if days is None:
        days = get_provider().get_trading_calendar(day.year, day.month)
    for row in days:
        if _as_date(row.get("date")) == day:
            return _row_is_open(row)
    return False


def session_snapshot(
    now: datetime | None = None,
    *,
    calendar_days: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = now or now_cn()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    day = current.date()
    clock = current.timetz().replace(tzinfo=None)
    trading = is_trading_day(day, calendar_days)
    if not trading:
        session = "closed"
    elif MORNING_OPEN <= clock < MORNING_CLOSE or AFTERNOON_OPEN <= clock < AFTERNOON_CLOSE:
        session = "open"
    elif MORNING_CLOSE <= clock < AFTERNOON_OPEN:
        session = "lunch"
    elif clock >= AFTERNOON_CLOSE:
        session = "postclose"
    else:
        session = "preopen"
    return {
        "now": current.isoformat(),
        "timezone": "Asia/Shanghai",
        "is_trading_day": trading,
        "session": session,
        "can_trade": session == "open",
        "can_review_evolve": trading and clock >= AFTERNOON_CLOSE,
        "postclose_due": trading and clock >= POSTCLOSE_TRIGGER,
    }


def require_trading_session() -> dict[str, Any]:
    info = session_snapshot()
    if not info["can_trade"]:
        raise ValueError(
            "MarketClosed: 仅交易日盘中（09:30–11:30、13:00–15:00）可下单，"
            f"当前 session={info['session']}"
        )
    return info


def require_review_window() -> dict[str, Any]:
    info = session_snapshot()
    if not info["can_review_evolve"]:
        raise ValueError(
            "ReviewWindowClosed: 仅交易日 15:00 之后可提出变更请求、沉淀经验或验收 CR，"
            f"当前 session={info['session']}"
        )
    return info
