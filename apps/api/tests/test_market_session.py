from datetime import datetime

from evo_api.services.market_session import CN_TZ, session_snapshot


def _days(open_dates: set[str]):
    return [{"date": d, "is_trading_day": True} for d in open_dates]


def test_session_windows():
    days = _days({"2026-10-09"})
    morning = session_snapshot(
        datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ), calendar_days=days
    )
    assert morning["session"] == "open" and morning["can_trade"]
    lunch = session_snapshot(
        datetime(2026, 10, 9, 12, 0, tzinfo=CN_TZ), calendar_days=days
    )
    assert lunch["session"] == "lunch" and not lunch["can_trade"]
    close_tick = session_snapshot(
        datetime(2026, 10, 9, 15, 0, tzinfo=CN_TZ), calendar_days=days
    )
    assert close_tick["session"] == "postclose"
    assert close_tick["can_review_evolve"]
    assert not close_tick["postclose_due"]
    due = session_snapshot(
        datetime(2026, 10, 9, 15, 5, tzinfo=CN_TZ), calendar_days=days
    )
    assert due["postclose_due"]
    weekend = session_snapshot(
        datetime(2026, 10, 10, 10, 0, tzinfo=CN_TZ), calendar_days=days
    )
    assert weekend["session"] == "closed"


def test_live_calendar_is_open_field():
    days = [{"date": "2026-10-09", "is_open": True}]
    info = session_snapshot(
        datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ), calendar_days=days
    )
    assert info["is_trading_day"] is True
