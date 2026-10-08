"""Market data facade over a-stock-data helpers (+ mock mode for tests/offline)."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any


class MarketDataError(Exception):
    """Raised when market data cannot be fetched (maps to DataGap upstream)."""


class MarketDataProvider:
    def __init__(self, mode: str | None = None) -> None:
        self.mode = (mode or os.getenv("MARKET_DATA_MODE", "mock")).lower()

    def get_market_snapshot(self, codes: list[str]) -> dict[str, Any]:
        codes = [c.strip() for c in codes if c and c.strip()]
        if not codes:
            raise MarketDataError("codes required")
        if self.mode == "mock":
            return {c: self._mock_quote(c) for c in codes}
        try:
            from market_data import asd_core

            raw = asd_core.tencent_quote(codes)
            if not raw:
                raise MarketDataError(f"empty quote for {codes}")
            return raw
        except MarketDataError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(str(exc)) from exc

    def get_etf_history(
        self,
        code: str,
        *,
        period: str = "day",
        adjust: str = "qfq",
        count: int = 60,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        code = code.strip()
        if not code:
            raise MarketDataError("code required")
        if self.mode == "mock":
            return self._mock_history(code, count=count)
        try:
            from market_data import asd_core

            df = asd_core.tencent_kline(
                code,
                period=period,
                adjust=adjust,
                start=start,
                end=end,
                count=count,
            )
            if df is None or getattr(df, "empty", True):
                raise MarketDataError(f"empty kline for {code}")
            records = df.reset_index(drop=True).to_dict(orient="records")
            # normalize timestamps to str
            for row in records:
                for k, v in list(row.items()):
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat()
                    elif hasattr(v, "item"):
                        row[k] = v.item()
            return records
        except MarketDataError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(str(exc)) from exc

    def get_trading_calendar(self, year: int, month: int) -> list[dict[str, Any]]:
        if self.mode == "mock":
            return self._mock_calendar(year, month)
        try:
            from market_data import asd_core

            df = asd_core.trading_calendar(year, month)
            if df is None or getattr(df, "empty", True):
                raise MarketDataError(f"empty calendar {year}-{month}")
            records = df.reset_index(drop=True).to_dict(orient="records")
            for row in records:
                for k, v in list(row.items()):
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat()
                    elif hasattr(v, "item"):
                        row[k] = v.item()
            return records
        except MarketDataError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(str(exc)) from exc

    def _mock_quote(self, code: str) -> dict[str, Any]:
        # Deterministic pseudo price from code digits
        digits = "".join(ch for ch in code if ch.isdigit()) or "510300"
        base = 1.0 + (int(digits[-4:]) % 500) / 100.0
        return {
            "code": code,
            "name": f"MOCK-ETF-{code}",
            "price": round(base, 3),
            "prev_close": round(base * 0.995, 3),
            "volume": 1_000_000,
            "amount": 1_000_000 * base,
            "is_stale": False,
            "source": "mock",
        }

    def _mock_history(self, code: str, *, count: int) -> list[dict[str, Any]]:
        snap = self._mock_quote(code)
        price = float(snap["price"])
        rows: list[dict[str, Any]] = []
        day = date.today()
        for i in range(count, 0, -1):
            d = day - timedelta(days=i)
            # skip weekends roughly
            if d.weekday() >= 5:
                continue
            p = round(price * (1 - 0.001 * (count - i)), 3)
            rows.append(
                {
                    "date": d.isoformat(),
                    "open": p,
                    "close": p,
                    "high": round(p * 1.01, 3),
                    "low": round(p * 0.99, 3),
                    "volume": 1_000_000,
                }
            )
        return rows[-count:]

    def _mock_calendar(self, year: int, month: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        d = date(year, month, 1)
        while d.month == month:
            rows.append(
                {
                    "date": d.isoformat(),
                    "is_trading_day": d.weekday() < 5,
                }
            )
            d += timedelta(days=1)
        return rows


_provider: MarketDataProvider | None = None


def reset_provider(mode: str | None = None) -> MarketDataProvider:
    """Recreate the singleton (e.g. after reading settings / changing env)."""
    global _provider
    if mode is not None:
        os.environ["MARKET_DATA_MODE"] = mode
    _provider = MarketDataProvider(mode=mode)
    return _provider


def get_provider() -> MarketDataProvider:
    global _provider
    desired = os.getenv("MARKET_DATA_MODE", "mock").lower()
    if _provider is None or _provider.mode != desired:
        _provider = MarketDataProvider(mode=desired)
    return _provider