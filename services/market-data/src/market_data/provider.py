"""Market data facade over a-stock-data helpers (+ mock mode for tests/offline)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Callable


class MarketDataError(Exception):
    """Raised when market data cannot be fetched (maps to DataGap upstream)."""


def _compact_bar(row: dict[str, Any]) -> dict[str, Any]:
    date_v = row.get("date") or row.get("datetime") or row.get("time")
    out = {
        "date": date_v,
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("close"),
        "volume": row.get("volume"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _parse_zd_fenbu(fenbu: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(fenbu, dict):
        iterable = fenbu.items()
    elif isinstance(fenbu, list):
        iterable = []
        for entry in fenbu:
            if isinstance(entry, dict):
                iterable.extend(entry.items())
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                iterable.append((entry[0], entry[1]))
    else:
        return items
    for key, value in iterable:
        try:
            count = int(float(value))
        except (TypeError, ValueError):
            continue
        bucket = str(key)
        try:
            n = int(float(bucket))
        except (TypeError, ValueError):
            label = bucket
        else:
            if n <= -10:
                label = "跌停"
            elif n >= 10:
                label = "涨停"
            elif n == 0:
                label = "平盘附近"
            elif n < 0:
                label = f"跌{abs(n)}%"
            else:
                label = f"涨{n}%"
        items.append({"bucket": bucket, "label": label, "count": count})
    items.sort(key=lambda r: float(r["bucket"]) if str(r["bucket"]).lstrip("-").isdigit() else 0)
    return items


STOCK_TYPES = ("stock", "etf", "convertible_bond")

# Eastmoney clist board filters
_EM_FS = {
    "etf": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
    "convertible_bond": "b:MK0354",
}

_MOCK_STOCK_UNIVERSE: list[dict[str, str]] = [
    {"code": "600519", "name": "贵州茅台"},
    {"code": "000858", "name": "五粮液"},
    {"code": "000001", "name": "平安银行"},
    {"code": "601318", "name": "中国平安"},
    {"code": "600036", "name": "招商银行"},
    {"code": "000333", "name": "美的集团"},
    {"code": "002415", "name": "海康威视"},
    {"code": "300750", "name": "宁德时代"},
    {"code": "601012", "name": "隆基绿能"},
    {"code": "600276", "name": "恒瑞医药"},
    {"code": "000568", "name": "泸州老窖"},
    {"code": "002594", "name": "比亚迪"},
    {"code": "300059", "name": "东方财富"},
    {"code": "601888", "name": "中国中免"},
    {"code": "600900", "name": "长江电力"},
    {"code": "601166", "name": "兴业银行"},
    {"code": "000651", "name": "格力电器"},
    {"code": "002475", "name": "立讯精密"},
    {"code": "300124", "name": "汇川技术"},
    {"code": "688981", "name": "中芯国际"},
    {"code": "600030", "name": "中信证券"},
    {"code": "601398", "name": "工商银行"},
    {"code": "000002", "name": "万科A"},
    {"code": "002352", "name": "顺丰控股"},
    {"code": "300274", "name": "阳光电源"},
    {"code": "600887", "name": "伊利股份"},
    {"code": "000725", "name": "京东方A"},
    {"code": "601899", "name": "紫金矿业"},
    {"code": "002714", "name": "牧原股份"},
    {"code": "300760", "name": "迈瑞医疗"},
]

_MOCK_ETF_UNIVERSE: list[dict[str, str]] = [
    {"code": "510300", "name": "沪深300ETF"},
    {"code": "510500", "name": "中证500ETF"},
    {"code": "159915", "name": "创业板ETF"},
    {"code": "588000", "name": "科创50ETF"},
    {"code": "512100", "name": "1000ETF"},
    {"code": "513100", "name": "纳指ETF"},
    {"code": "513500", "name": "标普500ETF"},
    {"code": "511010", "name": "国债ETF"},
    {"code": "518880", "name": "黄金ETF"},
    {"code": "512480", "name": "半导体ETF"},
    {"code": "159819", "name": "人工智能ETF"},
    {"code": "512880", "name": "证券ETF"},
    {"code": "512690", "name": "酒ETF"},
    {"code": "159992", "name": "创新药ETF"},
    {"code": "515790", "name": "光伏ETF"},
    {"code": "516160", "name": "新能源ETF"},
    {"code": "512010", "name": "医药ETF"},
    {"code": "512660", "name": "军工ETF"},
    {"code": "159985", "name": "豆粕ETF"},
    {"code": "511880", "name": "银华日利"},
]

# 常见宽基 ETF → 中证/国证指数（估值）
_ETF_TO_INDEX = {
    "510300": "000300",
    "159919": "000300",
    "510050": "000016",
    "510500": "000905",
    "159922": "000905",
    "159915": "399006",
    "588000": "000688",
    "588080": "000688",
    "512100": "000852",
}

_MOCK_CB_UNIVERSE: list[dict[str, str]] = [
    {"code": "110059", "name": "浦发转债"},
    {"code": "113052", "name": "兴业转债"},
    {"code": "113044", "name": "大族转债"},
    {"code": "127045", "name": "牧原转债"},
    {"code": "123107", "name": "温氏转债"},
    {"code": "128136", "name": "立讯转债"},
    {"code": "113050", "name": "南银转债"},
    {"code": "110085", "name": "通22转债"},
    {"code": "118031", "name": "天23转债"},
    {"code": "127073", "name": "天赐转债"},
    {"code": "123158", "name": "宙邦转债"},
    {"code": "113066", "name": "平银转债"},
    {"code": "110081", "name": "闻泰转债"},
    {"code": "127061", "name": "美锦转债"},
    {"code": "123119", "name": "康泰转2"},
]


def normalize_stock_type(value: str | None) -> str:
    raw = (value or "stock").strip().lower().replace("-", "_")
    aliases = {
        "stocks": "stock",
        "a_share": "stock",
        "ashare": "stock",
        "etfs": "etf",
        "fund": "etf",
        "cb": "convertible_bond",
        "bond": "convertible_bond",
        "convertible": "convertible_bond",
        "可转债": "convertible_bond",
        "转债": "convertible_bond",
    }
    stock_type = aliases.get(raw, raw)
    if stock_type not in STOCK_TYPES:
        raise MarketDataError(
            f"unsupported stock_type={value!r}; use one of {', '.join(STOCK_TYPES)}"
        )
    return stock_type


def quote_code_for_tencent(code: str, stock_type: str | None = None) -> str:
    """Ensure convertible-bond / ETF codes get the right Tencent market prefix."""
    digits = "".join(ch for ch in str(code) if ch.isdigit())[-6:]
    if not digits:
        return str(code)
    low = str(code).lower()
    if low.startswith(("sh", "sz", "bj")):
        return low
    st = (stock_type or "").lower()
    if st == "convertible_bond" or digits.startswith("11"):
        return f"sh{digits}"
    if digits.startswith("12"):
        return f"sz{digits}"
    if st == "etf" or digits.startswith(("51", "56", "58", "15", "16")):
        if digits.startswith(("15", "16", "159")):
            return f"sz{digits}"
        return f"sh{digits}"
    return digits


_EM_HOSTS = (
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://94.push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
)


_EM_HOSTS = (
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
)


class MarketDataProvider:
    def __init__(self, mode: str | None = None) -> None:
        self.mode = (mode or os.getenv("MARKET_DATA_MODE", "mock")).lower()
        self._universe_cache: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
        self._clist_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._http_cache: dict[str, tuple[float, Any]] = {}

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
        count: int = 20,
        start: str | None = None,
        end: str | None = None,
        tail: bool = True,
    ) -> list[dict[str, Any]]:
        code = code.strip()
        if not code:
            raise MarketDataError("code required")
        count = max(1, min(int(count), 40))
        if self.mode == "mock":
            records = self._mock_history(code, count=count)
        else:
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
                for row in records:
                    for k, v in list(row.items()):
                        if hasattr(v, "isoformat"):
                            row[k] = v.isoformat()
                        elif hasattr(v, "item"):
                            row[k] = v.item()
            except MarketDataError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise MarketDataError(str(exc)) from exc
        compact = [_compact_bar(row) for row in records]
        compact.sort(key=lambda r: str(r.get("date") or r.get("datetime") or ""))
        if start is None:
            compact = compact[-count:]
        if tail:
            # 最近 N 根；默认时间正序（最早→最新），latest 由上层单独给出
            return compact
        return compact[:count]

    def list_universe(
        self,
        *,
        stock_type: str = "stock",
        index_code: str | None = None,
        seed_codes: list[dict[str, Any] | str] | None = None,
    ) -> list[dict[str, Any]]:
        stock_type = normalize_stock_type(stock_type)
        index_code = (index_code or "000300").strip().zfill(6)
        cache_key = f"{stock_type}:{index_code}"
        cached = self._universe_cache.get(cache_key)
        if cached and datetime.now() - cached[0] < timedelta(hours=6):
            return cached[1]

        seeds = self._normalize_seed_codes(seed_codes)
        if self.mode == "mock":
            members = self._mock_universe(stock_type, seeds)
            self._universe_cache[cache_key] = (datetime.now(), members)
            return members

        try:
            if stock_type == "stock":
                members = self._list_index_members(index_code)
            else:
                members = self._list_eastmoney_market(stock_type)
                if seeds:
                    known = {m["code"] for m in members}
                    for item in seeds:
                        if item["code"] not in known:
                            members.append(item)
            if not members:
                raise MarketDataError(f"empty universe stock_type={stock_type}")
            self._universe_cache[cache_key] = (datetime.now(), members)
            return members
        except MarketDataError:
            if seeds:
                self._universe_cache[cache_key] = (datetime.now(), seeds)
                return seeds
            raise
        except Exception as exc:  # noqa: BLE001
            if seeds:
                self._universe_cache[cache_key] = (datetime.now(), seeds)
                return seeds
            raise MarketDataError(str(exc)) from exc

    def screen_market(
        self,
        *,
        stock_type: str = "stock",
        index_code: str = "000300",
        top_n: int = 30,
        extra_codes: list[str] | None = None,
        seed_codes: list[dict[str, Any] | str] | None = None,
    ) -> dict[str, Any]:
        stock_type = normalize_stock_type(stock_type)
        members = self.list_universe(
            stock_type=stock_type,
            index_code=index_code,
            seed_codes=seed_codes,
        )
        codes = [m["code"] for m in members]
        name_of = {m["code"]: m.get("name") for m in members}
        for code in extra_codes or []:
            digits = "".join(ch for ch in str(code) if ch.isdigit())[-6:]
            if digits and digits not in name_of:
                codes.append(digits)
                name_of[digits] = digits
        quotes = self._batch_quotes(codes, stock_type=stock_type)
        index_quotes = {}
        if stock_type == "stock":
            index_quotes = self.get_market_snapshot(["sh000300", "sz399006"])
        ranked: list[dict[str, Any]] = []
        for code, q in quotes.items():
            bare = "".join(ch for ch in str(code) if ch.isdigit())[-6:] or code
            if str(code).lower().startswith(("sh", "sz", "bj")) and bare in quotes:
                # prefer bare-code entries when both exist
                continue
            change = q.get("change_pct")
            if change is None and q.get("price") and (q.get("prev_close") or q.get("last_close")):
                prev = float(q.get("prev_close") or q.get("last_close") or 0.0)
                change = (float(q["price"]) - prev) / prev * 100.0 if prev else 0.0
            ranked.append(
                {
                    "code": bare,
                    "name": q.get("name") or name_of.get(bare) or bare,
                    "stock_type": stock_type,
                    "price": q.get("price"),
                    "change_pct": round(float(change or 0.0), 3),
                    "amount_wan": q.get("amount_wan") or q.get("amount"),
                    "pe_ttm": q.get("pe_ttm"),
                    "pb": q.get("pb"),
                    "turnover_pct": q.get("turnover_pct"),
                    "vol_ratio": q.get("vol_ratio"),
                }
            )
        ranked.sort(key=lambda r: abs(float(r.get("change_pct") or 0.0)), reverse=True)
        top_n = max(5, min(int(top_n), 80))
        return {
            "stock_type": stock_type,
            "index_code": index_code if stock_type == "stock" else None,
            "universe_size": len(members),
            "index_quotes": index_quotes,
            "candidates": ranked[:top_n],
        }

    def _mock_universe(
        self, stock_type: str, seeds: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        if seeds:
            return seeds
        if stock_type == "etf":
            return list(_MOCK_ETF_UNIVERSE)
        if stock_type == "convertible_bond":
            return list(_MOCK_CB_UNIVERSE)
        return list(_MOCK_STOCK_UNIVERSE)

    @staticmethod
    def _normalize_seed_codes(
        seed_codes: list[dict[str, Any] | str] | None,
    ) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in seed_codes or []:
            if isinstance(item, str):
                code = "".join(ch for ch in item if ch.isdigit())[-6:]
                if code:
                    out.append({"code": code, "name": code})
                continue
            code = "".join(ch for ch in str(item.get("code") or "") if ch.isdigit())[-6:]
            if not code:
                continue
            out.append({"code": code, "name": str(item.get("name") or code)})
        return out

    def _list_index_members(self, index_code: str) -> list[dict[str, Any]]:
        from market_data import asd_core

        df = asd_core._official_index_members(index_code, "csi", False)
        if df is None or getattr(df, "empty", True):
            raise MarketDataError(f"empty universe {index_code}")
        members = [
            {"code": str(row["code"]).zfill(6), "name": str(row.get("name") or "")}
            for row in df.to_dict(orient="records")
        ]
        if not members:
            raise MarketDataError(f"empty universe {index_code}")
        return members

    def _list_eastmoney_market(self, stock_type: str) -> list[dict[str, Any]]:
        fs = _EM_FS.get(stock_type)
        if not fs:
            raise MarketDataError(f"no eastmoney filter for {stock_type}")
        members: list[dict[str, Any]] = []
        page = 1
        page_size = 100
        while page <= 20:
            chunk = self._eastmoney_clist(fs=fs, page=page, page_size=page_size)
            if not chunk:
                break
            members.extend(chunk)
            if len(chunk) < page_size:
                break
            page += 1
        # de-dupe
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for item in members:
            if item["code"] in seen:
                continue
            seen.add(item["code"])
            unique.append(item)
        return unique

    def _http_json(self, urls: list[str], *, retries: int = 2, timeout: int = 5) -> Any:
        last: Exception | None = None
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        }
        # 绕过环境代理：本机代理常把东财 push2 打成 502/断连
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for url in urls:
            for attempt in range(retries):
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with opener.open(req, timeout=timeout) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    time.sleep(0.12 * (attempt + 1))
        raise MarketDataError(f"http json failed: {last}") from last

    def _eastmoney_urls(self, path: str, query: str) -> list[str]:
        return [f"{host}{path}?{query}" for host in _EM_HOSTS]

    def _eastmoney_clist(
        self,
        *,
        fs: str,
        page: int,
        page_size: int,
        fields: str = "f12,f14",
        fid: str = "f3",
    ) -> list[dict[str, Any]]:
        params = {
            "pn": str(page),
            "pz": str(page_size),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": fid,
            "fs": fs,
            "fields": fields,
        }
        query = urllib.parse.urlencode(params)
        cache_key = f"{fs}|{page}|{page_size}|{fields}|{fid}"
        try:
            payload = self._http_json(self._eastmoney_urls("/api/qt/clist/get", query))
        except MarketDataError:
            hit = self._clist_cache.get(cache_key)
            if hit:
                return hit[1]
            raise
        rows = ((payload or {}).get("data") or {}).get("diff") or []
        # 兼容旧调用：仅 code/name；扩展字段原样保留在 raw 调用里
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("f12") or "").zfill(6)
            name = str(row.get("f14") or "")
            if code and code != "000000":
                item = {"code": code, "name": name}
                item.update(row)
                out.append(item)
        if out:
            self._clist_cache[cache_key] = (time.time(), out)
        return out

    def _sina_hy_boards(self) -> list[dict[str, Any]]:
        url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=10) as resp:
            text = resp.read().decode("gbk", "replace")
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise MarketDataError("sina industry payload missing")
        obj = json.loads(match.group(0))
        rows: list[dict[str, Any]] = []
        for raw in obj.values():
            parts = str(raw).split(",")
            if len(parts) < 6:
                continue

            def _num(idx: int) -> float | None:
                if idx >= len(parts):
                    return None
                try:
                    return float(parts[idx])
                except (TypeError, ValueError):
                    return None

            rows.append(
                {
                    "code": parts[0],
                    "name": parts[1],
                    "count": _num(2),
                    "change_pct": _num(5),
                    "amount": _num(7),
                    "leader": parts[12] if len(parts) > 12 else "",
                    "leader_code": parts[8] if len(parts) > 8 else "",
                }
            )
        rows.sort(key=lambda r: float(r.get("change_pct") or 0), reverse=True)
        if not rows:
            raise MarketDataError("sina industry empty")
        return rows

    def _sina_industry_comparison(self, *, top_n: int, board: str) -> dict[str, Any]:
        rows = self._sina_hy_boards()
        ranked = [
            {
                "rank": i + 1,
                "name": r["name"],
                "code": r["code"],
                "change_pct": r.get("change_pct") or 0,
                "up_count": 0,
                "down_count": 0,
                "leader": r.get("leader") or "",
                "leader_change": 0,
            }
            for i, r in enumerate(rows)
        ]
        return {
            "board": board,
            "top": ranked[:top_n],
            "bottom": ranked[-top_n:] if ranked else [],
            "total": len(ranked),
            "source": "sina newSinaHy (eastmoney clist fallback)",
        }

    def _sina_board_turnover(
        self, *, top_n: int, board_type: str, period: str
    ) -> dict[str, Any]:
        rows = sorted(
            self._sina_hy_boards(),
            key=lambda r: float(r.get("amount") or 0),
            reverse=True,
        )
        mapped = [
            {
                "rank": i + 1,
                "name": r["name"],
                "code": r["code"],
                "change_pct": r.get("change_pct") or 0,
                "main_net": r.get("amount") or 0,
                "main_pct": 0,
                "leader": r.get("leader") or "",
            }
            for i, r in enumerate(rows)
        ]
        return {
            "board_type": board_type,
            "period": period,
            "total": len(mapped),
            "inflow": mapped[:top_n],
            "outflow": list(reversed(mapped[-top_n:])) if mapped else [],
            "metric": "turnover",
            "note": "东财资金流暂不可用，已降级为新浪行业成交额（非主力净流入）",
            "source": "sina newSinaHy",
        }

    def industry_comparison(self, *, top_n: int = 10, board: str = "industry") -> dict[str, Any]:
        """行业/概念涨跌排名（a-stock-data §3.7 industry_comparison 同源）。"""
        top_n = max(3, min(int(top_n), 30))
        fs = {"industry": "m:90+t:2", "concept": "m:90+t:3"}.get(board)
        if not fs:
            raise MarketDataError(f"board 须为 industry|concept，收到 {board!r}")
        if self.mode == "mock":
            rows = [
                {
                    "rank": i + 1,
                    "name": name,
                    "code": f"BK{i:04d}",
                    "change_pct": pct,
                    "up_count": 40 + i,
                    "down_count": 20 - i // 2,
                    "leader": leader,
                    "leader_change": pct + 5,
                }
                for i, (name, pct, leader) in enumerate(
                    [
                        ("半导体", 2.1, "某芯片"),
                        ("煤炭", 1.8, "某煤炭"),
                        ("非银金融", 1.2, "某券商"),
                        ("电力设备", -1.5, "某电力"),
                        ("消费电子", -2.0, "某电子"),
                    ]
                )
            ]
            return {
                "board": board,
                "top": rows[:top_n],
                "bottom": list(reversed(rows))[:top_n],
                "total": len(rows),
            }
        fields = "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207"
        try:
            items = self._eastmoney_clist(fs=fs, page=1, page_size=100, fields=fields, fid="f3")
        except Exception:
            return self._sina_industry_comparison(top_n=top_n, board=board)
        rows = []
        for i, item in enumerate(items):
            rows.append(
                {
                    "rank": i + 1,
                    "name": item.get("name") or item.get("f14") or "",
                    "code": item.get("code") or item.get("f12") or "",
                    "change_pct": item.get("f3", 0),
                    "up_count": item.get("f104", 0),
                    "down_count": item.get("f105", 0),
                    "leader": item.get("f140", ""),
                    "leader_change": item.get("f136", 0),
                }
            )
        return {
            "board": board,
            "top": rows[:top_n],
            "bottom": rows[-top_n:] if rows else [],
            "total": len(rows),
        }

    def board_fund_flow(
        self,
        *,
        board_type: str = "industry",
        period: str = "today",
        top_n: int = 10,
    ) -> dict[str, Any]:
        """板块资金流向（a-stock-data §3.8）。"""
        top_n = max(3, min(int(top_n), 30))
        board_fs = {"industry": "m:90+t:2", "concept": "m:90+t:3", "region": "m:90+t:1"}
        period_map = {
            "today": ("f62", "f62", "f184", "f3", "f204"),
            "5d": ("f164", "f164", "f165", "f109", "f257"),
            "10d": ("f174", "f174", "f175", "f160", None),
        }
        if board_type not in board_fs:
            raise MarketDataError(f"board_type 须为 {list(board_fs)}")
        if period not in period_map:
            raise MarketDataError(f"period 须为 {list(period_map)}")
        if self.mode == "mock":
            rows = [
                {
                    "rank": 1,
                    "name": "电子",
                    "code": "BK0447",
                    "change_pct": 1.2,
                    "main_net": 3.5e8,
                    "main_pct": 2.1,
                    "leader": "某电子",
                },
                {
                    "rank": 2,
                    "name": "半导体",
                    "code": "BK0481",
                    "change_pct": 2.0,
                    "main_net": 2.1e8,
                    "main_pct": 1.5,
                    "leader": "某芯片",
                },
                {
                    "rank": 3,
                    "name": "电力设备",
                    "code": "BK0428",
                    "change_pct": -1.1,
                    "main_net": -2.8e8,
                    "main_pct": -1.8,
                    "leader": "某电力",
                },
            ]
            return {
                "board_type": board_type,
                "period": period,
                "total": len(rows),
                "inflow": rows[:2],
                "outflow": [rows[-1]],
            }
        fid, f_main, f_pct, f_chg, f_leader = period_map[period]
        fields = ["f12", "f14", f_chg, f_main, f_pct]
        if f_leader:
            fields.append(f_leader)
        if period == "today":
            fields.extend(["f66", "f72", "f78", "f84"])
        try:
            items = self._eastmoney_clist(
                fs=board_fs[board_type],
                page=1,
                page_size=max(top_n * 2, 50),
                fields=",".join(dict.fromkeys(fields)),
                fid=fid,
            )
        except Exception:
            return self._sina_board_turnover(top_n=top_n, board_type=board_type, period=period)
        rows: list[dict[str, Any]] = []
        for i, it in enumerate(items):
            row = {
                "rank": i + 1,
                "name": it.get("name") or it.get("f14") or "",
                "code": it.get("code") or it.get("f12") or "",
                "change_pct": it.get(f_chg, 0),
                "main_net": it.get(f_main, 0),
                "main_pct": it.get(f_pct, 0),
                "leader": it.get(f_leader, "") if f_leader else "",
            }
            if period == "today":
                row.update(
                    {
                        "super_large_net": it.get("f66", 0),
                        "large_net": it.get("f72", 0),
                        "medium_net": it.get("f78", 0),
                        "small_net": it.get("f84", 0),
                    }
                )
            rows.append(row)
        inflow = rows[:top_n]
        outflow = list(reversed(rows[-top_n:])) if rows else []
        return {
            "board_type": board_type,
            "period": period,
            "total": len(rows),
            "inflow": inflow,
            "outflow": outflow,
        }

    def _try_section(self, name: str, fn: Callable[[], Any]) -> tuple[Any, dict[str, str] | None]:
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001
            return None, {"section": name, "error": str(exc)[:400]}

    def _index_secid(self, code: str) -> str:
        c = "".join(ch for ch in str(code) if ch.isdigit())[-6:].zfill(6)
        if c.startswith("399"):
            return f"0.{c}"
        return f"1.{c}"

    def _eastmoney_ulist(self, secids: list[str], *, fields: str) -> list[dict[str, Any]]:
        params = {
            "fltt": "2",
            "invt": "2",
            "fields": fields,
            "secids": ",".join(secids),
        }
        query = urllib.parse.urlencode(params)
        payload = self._http_json(self._eastmoney_urls("/api/qt/ulist.np/get", query))
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        return [row for row in diff if isinstance(row, dict)]

    def get_index_valuation(self, code: str) -> dict[str, Any]:
        """指数/宽基 ETF 估值：东财 PE/PB + 可选中证 PE 分位。"""
        raw = "".join(ch for ch in str(code) if ch.isdigit())[-6:]
        if not raw:
            raise MarketDataError("code required")
        index_code = _ETF_TO_INDEX.get(raw, raw)
        labels = {
            "000001": "上证指数",
            "399001": "深证成指",
            "000016": "上证50",
            "000300": "沪深300",
            "399006": "创业板指",
            "000905": "中证500",
            "000852": "中证1000",
            "000688": "科创50",
        }
        if self.mode == "mock":
            return {
                "code": raw,
                "index_code": index_code,
                "name": labels.get(index_code, index_code),
                "pe": 12.8,
                "pe_ttm": 12.8,
                "pb": 1.45,
                "pe_percentile": 42.0,
                "note": "ETF 报价 pe_ttm/pb 常为 0，请用本能力看对应指数估值",
                "source": "mock",
            }
        out: dict[str, Any] = {
            "code": raw,
            "index_code": index_code,
            "name": labels.get(index_code, index_code),
            "pe": None,
            "pe_ttm": None,
            "pb": None,
            "pe_percentile": None,
            "note": "ETF 快照 pe_ttm/pb 常为 0，已映射到对应指数",
            "source": "eastmoney_ulist",
        }
        try:
            rows = self._eastmoney_ulist(
                [self._index_secid(index_code)],
                fields="f2,f3,f9,f12,f14,f23,f115",
            )
            if rows:
                row = rows[0]
                out["name"] = row.get("f14") or out["name"]
                out["pe"] = row.get("f9")
                out["pe_ttm"] = row.get("f115") if row.get("f115") not in (None, "-", 0) else row.get("f9")
                out["pb"] = row.get("f23")
        except Exception as exc:  # noqa: BLE001
            out["ulist_error"] = str(exc)[:300]
        try:
            from market_data import asd_core

            df = asd_core.index_valuation(index_code)
            if df is not None and not getattr(df, "empty", True):
                recs = df.reset_index(drop=True).to_dict(orient="records")
                pes = [
                    float(r["pe_calculation"])
                    for r in recs
                    if r.get("pe_calculation") not in (None, "")
                ]
                if pes:
                    latest = pes[-1]
                    out["csi_pe"] = latest
                    out["pe_percentile"] = round(
                        sum(1 for x in pes if x <= latest) / len(pes) * 100, 1
                    )
                    if out.get("pe") in (None, 0, "-"):
                        out["pe"] = latest
                    out["source"] = "eastmoney_ulist+csi"
        except Exception as exc:  # noqa: BLE001
            out["csi_error"] = str(exc)[:300]
        if out.get("pe") in (None, 0, "-") and out.get("pb") in (None, 0, "-"):
            out["error"] = out.get("ulist_error") or out.get("csi_error") or "valuation empty"
        return out

    def get_market_breadth(self) -> dict[str, Any]:
        """全市场涨跌家数 + 涨跌幅分布直方图。"""
        if self.mode == "mock":
            hist = [
                {"bucket": "-11", "label": "跌停", "count": 8},
                {"bucket": "-7", "label": "跌7%", "count": 40},
                {"bucket": "-3", "label": "跌3%", "count": 320},
                {"bucket": "0", "label": "平", "count": 180},
                {"bucket": "3", "label": "涨3%", "count": 410},
                {"bucket": "7", "label": "涨7%", "count": 55},
                {"bucket": "11", "label": "涨停", "count": 22},
            ]
            up, down, flat = 900, 700, 180
            return {
                "up": up,
                "down": down,
                "flat": flat,
                "total": up + down + flat,
                "bias": "偏强",
                "histogram": hist,
                "source": "mock",
            }
        degraded: list[dict[str, str]] = []
        histogram: list[dict[str, Any]] = []
        try:
            payload = self._http_json(
                [
                    "https://push2ex.eastmoney.com/getTopicZDFenBu?"
                    + urllib.parse.urlencode(
                        {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt"}
                    )
                ]
            )
            fenbu = ((payload or {}).get("data") or {}).get("fenbu") or []
            histogram = _parse_zd_fenbu(fenbu)
        except Exception as exc:  # noqa: BLE001
            degraded.append({"section": "histogram", "error": str(exc)[:300]})

        up = down = flat = 0
        try:
            rows = self._eastmoney_ulist(
                ["1.000001", "0.399001"],
                fields="f12,f14,f104,f105,f106",
            )
            for row in rows:
                up += int(row.get("f104") or 0)
                down += int(row.get("f105") or 0)
                flat += int(row.get("f106") or 0)
        except Exception as exc:  # noqa: BLE001
            degraded.append({"section": "ulist_counts", "error": str(exc)[:300]})
            if histogram:
                for item in histogram:
                    key = str(item.get("bucket") or "")
                    n = int(item.get("count") or 0)
                    if key in ("0", "平"):
                        flat += n
                    elif key.startswith("-"):
                        down += n
                    else:
                        up += n

        if not histogram and up + down + flat == 0:
            raise MarketDataError("market breadth empty: " + str(degraded))
        total = up + down + flat
        bias = "偏强" if up > down * 1.05 else ("偏弱" if down > up * 1.05 else "均衡")
        return {
            "up": up,
            "down": down,
            "flat": flat,
            "total": total,
            "bias": bias,
            "histogram": histogram,
            "degraded": degraded,
            "source": "eastmoney getTopicZDFenBu + ulist",
        }

    def get_market_overview(self, *, top_n: int = 8) -> dict[str, Any]:
        """大盘综合：指数 + 行业/概念 + 资金流 + 估值/涨跌分布；单分项失败则降级。"""
        try:
            return self._get_market_overview(top_n=top_n)
        except Exception as exc:  # noqa: BLE001
            return {
                "as_of": datetime.now().isoformat(timespec="seconds"),
                "indices": [],
                "industry": {"leaders": [], "laggards": [], "total": 0},
                "concept": {"hot": [], "cold": [], "total": 0},
                "industry_fund_flow": {"period": "today", "inflow": [], "outflow": []},
                "degraded": [{"section": "all", "error": str(exc)[:400]}],
                "gaps": ["大盘综合构建失败，已降级"],
                "error": str(exc)[:400],
            }

    def _get_market_overview(self, *, top_n: int = 8) -> dict[str, Any]:
        top_n = max(3, min(int(top_n), 15))
        index_codes = ["sh000001", "sz399001", "sh000016", "sh000300", "sz399006"]
        index_labels = {
            "000001": "上证指数",
            "399001": "深证成指",
            "000016": "上证50",
            "000300": "沪深300",
            "399006": "创业板指",
        }
        try:
            raw_quotes = self.get_market_snapshot(index_codes)
        except MarketDataError:
            raw_quotes = {}
        indices: list[dict[str, Any]] = []
        for key, q in (raw_quotes or {}).items():
            if not isinstance(q, dict):
                continue
            bare = "".join(ch for ch in str(key) if ch.isdigit())[-6:] or str(key)
            indices.append(
                {
                    "code": bare,
                    "name": q.get("name") or index_labels.get(bare) or bare,
                    "price": q.get("price") or q.get("current"),
                    "change_pct": q.get("change_pct"),
                    "change_amt": q.get("change_amt"),
                    "amount_wan": q.get("amount_wan") or q.get("amount"),
                    "vol_ratio": q.get("vol_ratio"),
                }
            )

        degraded: list[dict[str, str]] = []
        industry, err = self._try_section(
            "industry_comparison", lambda: self.industry_comparison(top_n=top_n, board="industry")
        )
        if err:
            degraded.append(err)
            industry = {"top": [], "bottom": [], "total": 0}
        concept, err = self._try_section(
            "concept_comparison", lambda: self.industry_comparison(top_n=top_n, board="concept")
        )
        if err:
            degraded.append(err)
            concept = {"top": [], "bottom": [], "total": 0}
        fund, err = self._try_section(
            "board_fund_flow",
            lambda: self.board_fund_flow(board_type="industry", period="today", top_n=top_n),
        )
        if err:
            degraded.append(err)
            fund = {"inflow": [], "outflow": []}
        breadth, err = self._try_section("market_breadth", self.get_market_breadth)
        if err:
            degraded.append(err)
            breadth = None
        valuation, err = self._try_section(
            "index_valuation", lambda: self.get_index_valuation("000300")
        )
        if err:
            degraded.append(err)
            valuation = None

        up = 0
        down = 0
        for row in industry.get("top", []) + industry.get("bottom", []):
            try:
                up += int(row.get("up_count") or 0)
                down += int(row.get("down_count") or 0)
            except (TypeError, ValueError):
                continue
        breadth_proxy = {
            "note": "由行业板块涨跌家数汇总，存在重复计数，仅作市场情绪方向参考",
            "sector_up_sum": up,
            "sector_down_sum": down,
            "bias": (
                "偏强"
                if up > down * 1.05
                else ("偏弱" if down > up * 1.05 else "均衡")
            ),
        }

        gaps = ["尚无「大盘动态」文字快讯流"]
        if not breadth or not breadth.get("histogram"):
            gaps.insert(0, "涨跌分布直方图暂不可用，已降级")
        if not valuation:
            gaps.insert(0, "指数估值暂不可用，已降级")

        return {
            "as_of": datetime.now().isoformat(timespec="seconds"),
            "indices": indices,
            "index_valuation": valuation,
            "industry": {
                "leaders": industry.get("top", []),
                "laggards": industry.get("bottom", []),
                "total": industry.get("total", 0),
            },
            "concept": {
                "hot": concept.get("top", []),
                "cold": concept.get("bottom", []),
                "total": concept.get("total", 0),
            },
            "industry_fund_flow": {
                "period": "today",
                "inflow": fund.get("inflow", []),
                "outflow": fund.get("outflow", []),
            },
            "breadth": breadth,
            "breadth_proxy": breadth_proxy,
            "degraded": degraded,
            "sources": [
                "tencent_quote(indices)",
                "eastmoney clist (retry + push2delay)",
                "eastmoney ulist / getTopicZDFenBu",
                "csi index_valuation (optional)",
            ],
            "gaps": gaps,
        }

    def _batch_quotes(
        self, codes: list[str], *, stock_type: str | None = None
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        chunk = 80
        for i in range(0, len(codes), chunk):
            part = [quote_code_for_tencent(c, stock_type) for c in codes[i : i + chunk]]
            raw = self.get_market_snapshot(part)
            # normalize keys back to bare 6-digit codes
            for key, quote in raw.items():
                bare = "".join(ch for ch in str(key) if ch.isdigit())[-6:] or key
                out[bare] = quote
        return out

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
        prev = round(base * 0.995, 3)
        change_pct = round((base - prev) / prev * 100.0, 3) if prev else 0.0
        # vary mock change by code so screening has a ranking
        bump = ((int(digits[-3:]) % 17) - 8) / 10.0
        change_pct = round(change_pct + bump, 3)
        names = {
            m["code"]: m["name"]
            for m in (*_MOCK_STOCK_UNIVERSE, *_MOCK_ETF_UNIVERSE, *_MOCK_CB_UNIVERSE)
        }
        return {
            "code": code,
            "name": names.get(code, f"MOCK-{code}"),
            "price": round(base, 3),
            "prev_close": prev,
            "change_pct": change_pct,
            "volume": 1_000_000,
            "amount": 1_000_000 * base,
            "amount_wan": 1000.0 + (int(digits[-3:]) % 200),
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