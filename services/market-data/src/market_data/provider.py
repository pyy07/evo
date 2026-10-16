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


def _is_concept_noise(row: dict[str, Any]) -> bool:
    code = str(row.get("code") or "").upper()
    name = str(row.get("name") or "")
    if code in _CONCEPT_NOISE_CODES:
        return True
    return any(key in name for key in _CONCEPT_NOISE_NAME_KEYS)


def _board_level_rank(name: str) -> int:
    """板块层级：0=主层级，1=Ⅱ，2=Ⅲ；数值越小越优先保留。"""
    n = str(name or "").strip()
    if not n:
        return 0
    if re.search(r"(Ⅲ|III|III类|三级)\s*$", n):
        return 2
    if re.search(r"(Ⅱ|II)\s*$", n):
        return 1
    return 0


def _board_base_name(name: str) -> str:
    n = str(name or "").strip()
    n = re.sub(r"(Ⅲ|III|III类|三级)\s*$", "", n)
    n = re.sub(r"(Ⅱ|II)\s*$", "", n)
    return n.strip()


def _dedupe_board_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """按主层级去重：同 basename 优先保留无Ⅱ/Ⅲ后缀；同 code 只留一条。"""
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    order: list[str] = []
    seen_codes: set[str] = set()
    for row in rows or []:
        code = str(row.get("code") or "").strip()
        if code and code in seen_codes:
            continue
        name = str(row.get("name") or "")
        base = _board_base_name(name) or code or name
        level = _board_level_rank(name)
        if base not in best:
            best[base] = (level, row)
            order.append(base)
            if code:
                seen_codes.add(code)
            continue
        prev_level, _prev = best[base]
        if level < prev_level:
            prev_code = str(_prev.get("code") or "").strip()
            if prev_code:
                seen_codes.discard(prev_code)
            best[base] = (level, row)
            if code:
                seen_codes.add(code)
    return [best[k][1] for k in order]


# 东财行业榜常见「一级→子板块」别名（子名无法用前缀规则推断时）
_BOARD_CHILD_OF: dict[str, str] = {
    "半导体": "电子",
    "消费电子": "电子",
    "元件": "电子",
    "光学光电子": "电子",
    "其他电子": "电子",
    "通信设备": "通信",
    "通信网络设备及器件": "通信",
    "通信服务": "通信",
    "动力煤": "煤炭",
    "煤炭开采": "煤炭",
    "焦炭": "煤炭",
    "数字媒体": "传媒",
    "视频媒体": "传媒",
    "文字媒体": "传媒",
    "广告营销": "传媒",
    "电视广播": "传媒",
    "锂电池": "电池",
    "光伏设备": "电力设备",
    "风电设备": "电力设备",
    "电池": "电力设备",
}


def _is_board_child_of(child: str, parent: str) -> bool:
    c = _board_base_name(child)
    p = _board_base_name(parent)
    if not c or not p or c == p:
        return False
    if _BOARD_CHILD_OF.get(c) == p:
        return True
    # 煤炭开采 / 股份制银行：父名是子名的真前缀或真子串
    if len(p) >= 2 and c.startswith(p) and len(c) > len(p):
        return True
    if len(p) >= 2 and p in c and len(c) > len(p) and not c.startswith(p):
        # 避免过短误伤：要求父名至少 2 字且为完整词片段
        return True
    return False


def _dedupe_parent_child_boards(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """父子板块并存时保留父级（更短/一级），剔除子板块重复计数。"""
    rows = list(rows or [])
    drop: set[int] = set()
    for i, a in enumerate(rows):
        if i in drop:
            continue
        na = str(a.get("name") or "")
        for j, b in enumerate(rows):
            if i == j or j in drop:
                continue
            nb = str(b.get("name") or "")
            if _is_board_child_of(nb, na):
                drop.add(j)
            elif _is_board_child_of(na, nb):
                drop.add(i)
                break
    return [r for i, r in enumerate(rows) if i not in drop]


def _member_count(row: dict[str, Any]) -> int:
    try:
        up = int(row.get("up_count") or 0)
        down = int(row.get("down_count") or 0)
        return max(0, up + down)
    except (TypeError, ValueError):
        return 0


def _compact_board_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """概览里只保留必要字段，避免 tool 输出被 6k 截断后丢掉资金流尾部。"""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        item = {
            "rank": row.get("rank"),
            "code": row.get("code"),
            "name": row.get("name"),
            "change_pct": row.get("change_pct"),
        }
        if row.get("main_net") is not None:
            item["main_net"] = row.get("main_net")
        if row.get("main_pct") is not None:
            item["main_pct"] = row.get("main_pct")
        if row.get("leader"):
            item["leader"] = row.get("leader")
        if row.get("up_count") is not None:
            item["up_count"] = row.get("up_count")
        if row.get("down_count") is not None:
            item["down_count"] = row.get("down_count")
        out.append(item)
    return out


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
    {"code": "510050", "name": "上证50ETF"},
    {"code": "510500", "name": "中证500ETF"},
    {"code": "159915", "name": "创业板ETF"},
    {"code": "588000", "name": "科创50ETF"},
    {"code": "512100", "name": "1000ETF"},
    {"code": "512890", "name": "红利低波ETF"},
    {"code": "563020", "name": "中证红利ETF"},
    {"code": "515080", "name": "中证红利ETF"},
    {"code": "510880", "name": "红利ETF"},
    {"code": "159905", "name": "深红利ETF"},
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

# 概念榜里的交易统计/财报统计型板块（非主题），干扰风格/主题判断
_CONCEPT_NOISE_CODES = frozenset(
    {"BK1645", "BK1675", "BK1050", "BK0815", "BK0816", "BK1199", "BK1750"}
)
_CONCEPT_NOISE_NAME_KEYS = (
    "打板",
    "打二板",
    "涨停",
    "跌停",
    "连板",
    "炸板",
    "新高",
    "新低",
    "一字",
    "昨日",
    "晋级",
    "破板",
    "预减",
    "预增",
    "扭亏",
    "增亏",
    "中报",
    "季报",
    "年报",
    "业绩预告",
    "业绩快报",
)

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
    "512890": "930955",  # 中证红利低波近似映射；无中证文件时仅作代码锚
    "563020": "000922",
    "515080": "000922",
    "510880": "000015",
    "159905": "399324",
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

# 国家队代理：历史上常被汇金/证金/社保披露持有的宽基 ETF（非实时归因名单）
_NT_SEED_ETFS: list[dict[str, str]] = [
    {"code": "510050", "name": "上证50ETF"},
    {"code": "510300", "name": "沪深300ETF华泰柏瑞"},
    {"code": "510310", "name": "沪深300ETF易方达"},
    {"code": "510500", "name": "中证500ETF"},
    {"code": "159919", "name": "沪深300ETF华夏"},
    {"code": "159922", "name": "中证500ETF嘉实"},
    {"code": "588000", "name": "科创50ETF"},
    {"code": "512100", "name": "中证1000ETF"},
]

_NT_HOLDER_KEYWORDS = (
    "中央汇金",
    "汇金资管",
    "汇金投资",
    "中国证券金融",
    "证金",
    "全国社保",
    "社保基金",
    "基本养老",
    "社保",
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
        po: str = "1",
    ) -> list[dict[str, Any]]:
        # po: "1"=降序（强/流入端），"0"=升序（弱/流出端）
        po_v = "0" if str(po) in {"0", "asc", "ascending"} else "1"
        params = {
            "pn": str(page),
            "pz": str(page_size),
            "po": po_v,
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": fid,
            "fs": fs,
            "fields": fields,
        }
        query = urllib.parse.urlencode(params)
        cache_key = f"{fs}|{page}|{page_size}|{fields}|{fid}|{po_v}"
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

    @staticmethod
    def _map_board_rank_rows(
        items: list[dict[str, Any]],
        *,
        change_key: str = "f3",
        up_key: str = "f104",
        down_key: str = "f105",
        leader_key: str = "f140",
        leader_chg_key: str = "f136",
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i, item in enumerate(items):
            rows.append(
                {
                    "rank": i + 1,
                    "name": item.get("name") or item.get("f14") or "",
                    "code": item.get("code") or item.get("f12") or "",
                    "change_pct": item.get(change_key, 0),
                    "up_count": item.get(up_key, 0),
                    "down_count": item.get(down_key, 0),
                    "leader": item.get(leader_key, ""),
                    "leader_change": item.get(leader_chg_key, 0),
                }
            )
        return rows

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

    def _sina_industry_comparison(
        self, *, top_n: int, board: str, min_member_count: int = 3
    ) -> dict[str, Any]:
        rows = _dedupe_parent_child_boards(_dedupe_board_rows(self._sina_hy_boards()))
        ranked = []
        low_excluded: list[dict[str, Any]] = []
        for r in rows:
            try:
                mc = int(float(r.get("count") or 0))
            except (TypeError, ValueError):
                mc = 0
            item = {
                "name": r["name"],
                "code": r["code"],
                "change_pct": r.get("change_pct") or 0,
                "up_count": 0,
                "down_count": 0,
                "member_count": mc,
                "low_sample": mc > 0 and mc < min_member_count,
                "leader": r.get("leader") or "",
                "leader_change": 0,
            }
            if item["low_sample"]:
                low_excluded.append(item)
            else:
                ranked.append(item)
        if len(ranked) < top_n:
            ranked.extend(low_excluded[: top_n - len(ranked)])
        top = [{**r, "rank": i + 1} for i, r in enumerate(ranked[:top_n])]
        all_sorted = sorted(
            ranked + [x for x in low_excluded if x not in ranked],
            key=lambda x: float(x.get("change_pct") or 0),
        )
        # weakest first from full set after filter preference
        weak_pool = [r for r in all_sorted if not r.get("low_sample")] or all_sorted
        laggards = [{**r, "rank": i + 1} for i, r in enumerate(weak_pool[:top_n])]
        return {
            "board": board,
            "top": top,
            "bottom": laggards,
            "total": len(ranked) + len(low_excluded),
            "universe_size": len(rows),
            "min_member_count": min_member_count,
            "low_sample_excluded_top": [
                {"code": r.get("code"), "name": r.get("name"), "member_count": r.get("member_count")}
                for r in low_excluded
            ],
            "sort": "change_pct",
            "bottom_order": "weakest_first",
            "universe_note": (
                f"新浪行业全样本；主榜优先 member_count>={min_member_count}；"
                "bottom 为涨跌幅最弱端"
            ),
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
            for i, r in enumerate(_dedupe_board_rows(rows))
        ]
        return {
            "board_type": board_type,
            "period": period,
            "total": len(mapped),
            "inflow": mapped[:top_n],
            "outflow": list(reversed(mapped[-top_n:])) if mapped else [],
            "metric": "turnover",
            "outflow_basis": "least_turnover",
            "note": "东财资金流暂不可用，已降级为新浪行业成交额（非主力净流入）",
            "source": "sina newSinaHy",
        }

    def industry_comparison(
        self,
        *,
        top_n: int = 10,
        board: str = "industry",
        min_member_count: int = 3,
    ) -> dict[str, Any]:
        """行业/概念涨跌排名（a-stock-data §3.7 industry_comparison 同源）。"""
        top_n = max(3, min(int(top_n), 30))
        min_member_count = max(1, min(int(min_member_count), 50))
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
                    "member_count": 60 + i,
                    "low_sample": False,
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
            bottom = list(reversed(rows))[:top_n]
            for i, row in enumerate(bottom):
                bottom[i] = {**row, "rank": i + 1}
            return {
                "board": board,
                "top": rows[:top_n],
                "bottom": bottom,
                "total": len(rows),
                "universe_size": len(rows),
                "min_member_count": min_member_count,
                "sort": "change_pct",
                "bottom_order": "weakest_first",
                "universe_note": "mock 全样本；bottom 为涨跌幅最弱端",
            }
        fields = "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207"
        try:
            # 两端分别按涨跌幅降/升序取样，避免「前100强尾部」冒充全市场最弱
            strong = self._eastmoney_clist(
                fs=fs, page=1, page_size=100, fields=fields, fid="f3", po="1"
            )
            weak = self._eastmoney_clist(
                fs=fs, page=1, page_size=100, fields=fields, fid="f3", po="0"
            )
        except Exception:
            return self._sina_industry_comparison(
                top_n=top_n, board=board, min_member_count=min_member_count
            )
        top_rows = _dedupe_parent_child_boards(
            _dedupe_board_rows(self._map_board_rank_rows(strong))
        )
        bottom_rows = _dedupe_parent_child_boards(
            _dedupe_board_rows(self._map_board_rank_rows(weak))
        )
        filtered_noise: list[dict[str, Any]] = []
        if board == "concept":
            kept_top: list[dict[str, Any]] = []
            kept_bottom: list[dict[str, Any]] = []
            for r in top_rows:
                if _is_concept_noise(r):
                    filtered_noise.append({"code": r.get("code"), "name": r.get("name"), "side": "top"})
                else:
                    kept_top.append(r)
            for r in bottom_rows:
                if _is_concept_noise(r):
                    filtered_noise.append({"code": r.get("code"), "name": r.get("name"), "side": "bottom"})
                else:
                    kept_bottom.append(r)
            top_rows, bottom_rows = kept_top, kept_bottom

        def _annotate(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            kept: list[dict[str, Any]] = []
            low: list[dict[str, Any]] = []
            for r in rows:
                mc = _member_count(r)
                item = {**r, "member_count": mc, "low_sample": mc < min_member_count}
                if item["low_sample"]:
                    low.append(item)
                else:
                    kept.append(item)
            return kept, low

        top_kept, top_low = _annotate(top_rows)
        bottom_kept, bottom_low = _annotate(bottom_rows)
        # 主榜优先够样本；不足时用 low_sample 补齐并保留标注
        def _fill(kept: list[dict[str, Any]], low: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = list(kept)
            if len(out) < top_n:
                out.extend(low[: top_n - len(out)])
            return [{**r, "rank": i + 1} for i, r in enumerate(out[:top_n])]

        top = _fill(top_kept, top_low)
        bottom = _fill(bottom_kept, bottom_low)
        result: dict[str, Any] = {
            "board": board,
            "top": top,
            "bottom": bottom,
            "total": max(len(top_kept) + len(top_low), len(bottom_kept) + len(bottom_low)),
            "universe_size": max(len(top_rows), len(bottom_rows)),
            "min_member_count": min_member_count,
            "low_sample_excluded_top": [
                {"code": r.get("code"), "name": r.get("name"), "member_count": r.get("member_count")}
                for r in top_low
            ],
            "sort": "change_pct",
            "bottom_order": "weakest_first",
            "universe_note": (
                "东财行业/概念板块榜按涨跌幅两端取样（非前100强尾部）；"
                "已按主层级/父子板块去重；"
                f"主榜优先 member_count>={min_member_count}（up+down），不足则带 low_sample=true 回填"
                + ("；概念已过滤交易/财报统计标签" if board == "concept" else "")
            ),
            "source": "eastmoney clist",
        }
        if board == "concept":
            result["filtered_noise"] = filtered_noise
            result["filtered_noise_count"] = len(filtered_noise)
        return result

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
                "outflow_basis": "main_net_negative",
                "note": "mock：outflow 为 main_net<0",
            }
        fid, f_main, f_pct, f_chg, f_leader = period_map[period]
        fields = ["f12", "f14", f_chg, f_main, f_pct]
        if f_leader:
            fields.append(f_leader)
        if period == "today":
            fields.extend(["f66", "f72", "f78", "f84"])
        field_str = ",".join(dict.fromkeys(fields))
        page_size = max(top_n * 3, 50)

        def _map_flow(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            mapped: list[dict[str, Any]] = []
            for it in items:
                row = {
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
                mapped.append(row)
            return _dedupe_parent_child_boards(_dedupe_board_rows(mapped))

        try:
            inflow_items = self._eastmoney_clist(
                fs=board_fs[board_type],
                page=1,
                page_size=page_size,
                fields=field_str,
                fid=fid,
                po="1",
            )
            outflow_items = self._eastmoney_clist(
                fs=board_fs[board_type],
                page=1,
                page_size=page_size,
                fields=field_str,
                fid=fid,
                po="0",
            )
        except Exception:
            return self._sina_board_turnover(top_n=top_n, board_type=board_type, period=period)
        inflow_rows = _map_flow(inflow_items)
        outflow_rows = _map_flow(outflow_items)
        if board_type == "concept":
            inflow_rows = [r for r in inflow_rows if not _is_concept_noise(r)]
            outflow_rows = [r for r in outflow_rows if not _is_concept_noise(r)]

        def _net(row: dict[str, Any]) -> float:
            try:
                return float(row.get("main_net") or 0)
            except (TypeError, ValueError):
                return 0.0

        true_out = [r for r in outflow_rows if _net(r) < 0]
        if true_out:
            outflow_sel = true_out[:top_n]
            outflow_basis = "main_net_negative"
            outflow_note = "真实主力净流出（main_net<0），按净流出从大到小"
        else:
            outflow_sel = outflow_rows[:top_n]
            outflow_basis = "least_inflow"
            outflow_note = "当日样本无 main_net<0，返回净流入最少尾部（数值仍可能为正）"

        inflow = [{**r, "rank": i + 1} for i, r in enumerate(inflow_rows[:top_n])]
        outflow = [{**r, "rank": i + 1} for i, r in enumerate(outflow_sel)]
        return {
            "board_type": board_type,
            "period": period,
            "total": max(len(inflow_rows), len(outflow_rows)),
            "inflow": inflow,
            "outflow": outflow,
            "outflow_basis": outflow_basis,
            "note": outflow_note + "；已按Ⅱ/Ⅲ与父子板块去重（保留一级）",
            "source": "eastmoney clist",
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

    @staticmethod
    def _num_or_none(value: Any) -> float | None:
        if value in (None, "", "-", "--", "None"):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:  # NaN
            return None
        return number

    def get_index_valuation(self, code: str) -> dict[str, Any]:
        """指数/宽基 ETF 估值：优先中证 PE/股息率，东财仅作名称补充。

        说明：东财指数报价 pe/pb 常为 '-'；中证 indicator.xls 不含 PB。
        pe_percentile 基于中证近期文件窗口（通常约 20 个交易日），不是全历史分位。
        """
        raw = "".join(ch for ch in str(code) if ch.isdigit())[-6:]
        if not raw:
            raise MarketDataError("code required")
        index_code = _ETF_TO_INDEX.get(raw, raw)
        labels = {
            "000001": "上证指数",
            "399001": "深证成指",
            "000015": "红利指数",
            "000016": "上证50",
            "000300": "沪深300",
            "399006": "创业板指",
            "000905": "中证500",
            "000852": "中证1000",
            "000688": "科创50",
            "399303": "国证2000",
            "000922": "中证红利",
        }
        mapped_name = labels.get(index_code) or labels.get(raw) or index_code
        if self.mode == "mock":
            return {
                "code": raw,
                "index_code": index_code,
                "name": mapped_name,
                "pe": 12.8,
                "pe_ttm": 12.8,
                "pb": None,
                "pe_percentile": 42.0,
                "pe_percentile_window": 20,
                "pe_percentile_basis": "csi_recent_file",
                "pe_percentile_window_start": (date.today() - timedelta(days=30)).isoformat(),
                "pe_percentile_window_end": date.today().isoformat(),
                "pe_percentile_1y": None,
                "pe_percentile_3y": None,
                "pb": None,
                "pb_status": "unavailable",
                "dividend_yield_percent": 2.5,
                "as_of": date.today().isoformat(),
                "degraded": False,
                "note": (
                    "ETF 快照 pe_ttm/pb 常为 0/-，请用本能力。"
                    "PB 中证未提供；pe_percentile 仅为中证近期文件窗口分位，非 1年/3年/全历史。"
                ),
                "source": "mock",
            }
        out: dict[str, Any] = {
            "code": raw,
            "index_code": index_code,
            "name": mapped_name,
            "pe": None,
            "pe_ttm": None,
            "pb": None,
            "pb_status": "unavailable",
            "pe_percentile": None,
            "pe_percentile_window": None,
            "pe_percentile_basis": None,
            "pe_percentile_window_start": None,
            "pe_percentile_window_end": None,
            "pe_percentile_1y": None,
            "pe_percentile_3y": None,
            "dividend_yield_percent": None,
            "as_of": None,
            "degraded": False,
            "note": (
                "ETF/指数快照 pe_ttm、pb 常为 0 或 '-'；"
                "PE 与股息率来自中证近期估值文件。"
                "pb_status=unavailable（中证 indicator.xls 无 PB）；"
                "pe_percentile 仅为该文件近期窗口分位，1y/3y 全历史分位暂无同源数据。"
                "name 优先本地映射，东财 ulist 仅作补充；ulist 失败标 degraded，不把代码当名称。"
            ),
            "source": None,
        }
        try:
            rows = self._eastmoney_ulist(
                [self._index_secid(index_code)],
                fields="f2,f3,f9,f12,f14,f23,f115",
            )
            if rows:
                row = rows[0]
                em_name = str(row.get("f14") or "").strip()
                # 仅当本地无映射时才采用东财名称，避免失败路径污染
                if em_name and index_code not in labels and raw not in labels:
                    out["name"] = em_name
                elif em_name and out["name"] == index_code:
                    out["name"] = em_name
                pe = self._num_or_none(row.get("f9"))
                pe_ttm = self._num_or_none(row.get("f115")) or pe
                pb = self._num_or_none(row.get("f23"))
                if pe is not None:
                    out["pe"] = pe
                if pe_ttm is not None:
                    out["pe_ttm"] = pe_ttm
                if pb is not None:
                    out["pb"] = pb
                out["source"] = "eastmoney_ulist"
        except Exception as exc:  # noqa: BLE001
            out["ulist_error"] = str(exc)[:300]
            out["degraded"] = True
            out["ulist_status"] = "unavailable"
            # 保持 mapped_name，绝不回退成裸代码误导下游
        try:
            from market_data import asd_core

            df = asd_core.index_valuation(index_code)
            if df is not None and not getattr(df, "empty", True):
                recs = df.reset_index(drop=True).to_dict(orient="records")
                pes = [
                    float(r["pe_calculation"])
                    for r in recs
                    if self._num_or_none(r.get("pe_calculation")) is not None
                ]
                if pes:
                    latest = pes[-1]
                    out["csi_pe"] = latest
                    out["pe"] = latest
                    out["pe_ttm"] = latest
                    out["pe_percentile"] = round(
                        sum(1 for x in pes if x <= latest) / len(pes) * 100, 1
                    )
                    out["pe_percentile_window"] = len(pes)
                    out["pe_percentile_basis"] = "csi_recent_file"
                    dates = [str(r.get("date") or "") for r in recs if r.get("date")]
                    out["pe_percentile_window_start"] = dates[0] if dates else None
                    out["pe_percentile_window_end"] = dates[-1] if dates else None
                    # 显式占位：避免 Agent 把近期窗口误当成 1年/全历史分位
                    out["pe_percentile_1y"] = None
                    out["pe_percentile_3y"] = None
                    last = recs[-1]
                    out["as_of"] = last.get("date")
                    out["dividend_yield_percent"] = self._num_or_none(
                        last.get("dividend_yield_calculation_percent")
                    )
                    out["pe_total"] = self._num_or_none(last.get("pe_total"))
                    out["source"] = (
                        "csi+eastmoney_ulist" if out.get("source") else "csi"
                    )
        except Exception as exc:  # noqa: BLE001
            out["csi_error"] = str(exc)[:300]
        if out.get("pe") is None and out.get("pb") is None:
            out["error"] = out.get("csi_error") or out.get("ulist_error") or "valuation empty"
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

        industry_leaders = _dedupe_board_rows(industry.get("top"))
        industry_laggards = _dedupe_board_rows(industry.get("bottom"))
        # industry_comparison(concept) 已过滤噪声；此处再兜底一遍并汇总计数
        concept_hot = _dedupe_board_rows(
            [r for r in (concept.get("top") or []) if not _is_concept_noise(r)]
        )
        concept_cold = _dedupe_board_rows(
            [r for r in (concept.get("bottom") or []) if not _is_concept_noise(r)]
        )
        filtered_noise = list(concept.get("filtered_noise") or [])
        if not filtered_noise:
            filtered_noise = [
                {"code": r.get("code"), "name": r.get("name")}
                for r in (concept.get("top") or []) + (concept.get("bottom") or [])
                if _is_concept_noise(r)
            ]
        # 概览内资金流只保留少量摘要，避免 6k 截断；完整列表用 get_board_fund_flow
        flow_n = min(top_n, 4)

        hsgt, err = self._try_section("hsgt_flow", self.get_hsgt_flow)
        if err:
            degraded.append(err)
            hsgt = None

        gaps = ["尚无「大盘动态」文字快讯流"]
        if not breadth or not breadth.get("histogram"):
            gaps.insert(0, "涨跌分布直方图暂不可用，已降级")
        if not valuation:
            gaps.insert(0, "指数估值暂不可用，已降级")
        if valuation and valuation.get("pb_status") == "unavailable":
            gaps.insert(0, "指数 PB 暂无同源数据（pb_status=unavailable）")
        if valuation and valuation.get("pe_percentile_1y") is None:
            gaps.insert(0, "指数 1年/3年 PE 分位暂无同源数据，仅有近期窗口分位")
        if not hsgt:
            gaps.insert(0, "北向/沪深港通资金暂不可用")
        elif hsgt.get("usable_for_style") is False:
            gaps.insert(0, "北向净买入不可用（仅有成交额），不可作外资风格验证")

        return {
            "as_of": datetime.now().isoformat(timespec="seconds"),
            "indices": indices,
            "index_valuation": valuation,
            "hsgt": hsgt,
            "industry": {
                "leaders": _compact_board_rows(industry_leaders[:top_n]),
                "laggards": _compact_board_rows(industry_laggards[:top_n]),
                "total": industry.get("total", 0),
                "note": "已按主层级去重（同名优先保留无Ⅱ/Ⅲ后缀）；laggards 为涨跌幅最弱端",
            },
            "concept": {
                "hot": _compact_board_rows(concept_hot[:top_n]),
                "cold": _compact_board_rows(concept_cold[:top_n]),
                "total": concept.get("total", 0),
                "filtered_noise_count": len(filtered_noise),
                "note": "已过滤打板/涨停/新高及季报预减/中报扭亏等统计标签",
            },
            "industry_fund_flow": {
                "period": "today",
                "inflow": _compact_board_rows(_dedupe_board_rows(fund.get("inflow"))[:flow_n]),
                "outflow": _compact_board_rows(_dedupe_board_rows(fund.get("outflow"))[:flow_n]),
                "top_n": flow_n,
                "note": f"概览仅摘要前 {flow_n}；完整主力分项请调 get_board_fund_flow",
            },
            "breadth": breadth,
            "breadth_proxy": breadth_proxy,
            "degraded": degraded,
            "sources": [
                "tencent_quote(indices)",
                "eastmoney clist (retry + push2delay) / sina fallback",
                "eastmoney ulist / getTopicZDFenBu / RPT_MUTUAL_DEALAMT / kamt",
                "csi index_valuation (optional)",
            ],
            "gaps": gaps,
        }

    def _eastmoney_datacenter(
        self, report_name: str, *, page_size: int = 5, extra: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        params = {
            "reportName": report_name,
            "columns": "ALL",
            "sortColumns": "TRADE_DATE",
            "sortTypes": "-1",
            "pageSize": str(page_size),
            "pageNumber": "1",
        }
        if extra:
            params.update(extra)
        query = urllib.parse.urlencode(params)
        payload = self._http_json(
            [f"https://datacenter-web.eastmoney.com/api/data/v1/get?{query}"]
        )
        if not (payload or {}).get("success"):
            raise MarketDataError(
                f"datacenter {report_name}: {(payload or {}).get('message') or 'failed'}"
            )
        rows = ((payload or {}).get("result") or {}).get("data") or []
        return [r for r in rows if isinstance(r, dict)]

    @staticmethod
    def _net_buy_usable(buy: float | None, sell: float | None, net: float | None) -> bool:
        """仅在买卖成交可核对时采信净买入；额度字段不可当净流入。"""
        if net is None or buy is None or sell is None:
            return False
        if buy == 0 and sell == 0:
            return False
        # 允许撮合/四舍五入误差
        return abs((buy - sell) - net) <= max(1.0, abs(net) * 0.05)

    def get_hsgt_flow(self) -> dict[str, Any]:
        """沪深港通资金：优先披露成交额；北向净买入不可用时明确 degraded。

        注意：东财 kamt 北向 dayNetAmtIn/monthNetAmtIn 在监管披露收紧后常等于额度或为 0，
        不能当作净买入。本能力不编造净买入。
        """
        if self.mode == "mock":
            return {
                "as_of": date.today().isoformat(),
                "northbound": {
                    "deal_amt_wan": 248_023.19,
                    "sh_deal_amt_wan": 117_452.13,
                    "sz_deal_amt_wan": 130_571.06,
                    "net_buy_wan": None,
                    "net_buy_status": "unavailable",
                },
                "southbound": {
                    "net_buy_wan": 51_288.69,
                    "net_buy_status": "ok",
                    "sh_net_buy_wan": 64_573.86,
                    "sz_net_buy_wan": -13_285.17,
                },
                "usable_for_style": False,
                "unit": "万元",
                "note": "mock：北向仅成交额；净买入不可用时 usable_for_style=false",
                "source": "mock",
            }

        degraded: list[dict[str, str]] = []
        as_of: str | None = None
        north: dict[str, Any] = {
            "deal_amt_wan": None,
            "sh_deal_amt_wan": None,
            "sz_deal_amt_wan": None,
            "net_buy_wan": None,
            "net_buy_status": "unavailable",
        }
        south: dict[str, Any] = {
            "net_buy_wan": None,
            "net_buy_status": "unavailable",
        }

        try:
            rows = self._eastmoney_datacenter("RPT_MUTUAL_DEALAMT", page_size=3)
            if not rows:
                raise MarketDataError("RPT_MUTUAL_DEALAMT empty")
            latest = rows[0]
            as_of = str(latest.get("TRADE_DATE") or "")[:10] or None
            north["deal_amt_wan"] = self._num_or_none(latest.get("NF_DEAL_AMT"))
            north["sh_deal_amt_wan"] = self._num_or_none(latest.get("SSC_DEAL_AMT"))
            north["sz_deal_amt_wan"] = self._num_or_none(latest.get("ST_DEAL_AMT"))
            north["lead_stock"] = latest.get("NF_LEAD_STOCKS")
            north["quota_text"] = latest.get("NF_QUOTA_BALANCE")
        except Exception as exc:  # noqa: BLE001
            degraded.append({"section": "deal_amt", "error": str(exc)[:300]})

        try:
            query = urllib.parse.urlencode(
                {
                    "fields1": "f1,f2,f3,f4",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                    "ut": "b2884a393a59ad64002292a3e90d46a5",
                }
            )
            payload = self._http_json(self._eastmoney_urls("/api/qt/kamt/get", query))
            data = (payload or {}).get("data") or {}
            # 北向：仅当买卖成交可核对才采信 netBuyAmt；否则保持 unavailable（勿用 dayNetAmtIn/额度）
            for key, side in (("hk2sh", "sh"), ("hk2sz", "sz")):
                row = data.get(key) or {}
                buy = self._num_or_none(row.get("buyAmt"))
                sell = self._num_or_none(row.get("sellAmt"))
                net = self._num_or_none(row.get("netBuyAmt"))
                if self._net_buy_usable(buy, sell, net):
                    north[f"{side}_net_buy_wan"] = net
            sh_n = north.get("sh_net_buy_wan")
            sz_n = north.get("sz_net_buy_wan")
            if sh_n is not None or sz_n is not None:
                north["net_buy_wan"] = round((sh_n or 0.0) + (sz_n or 0.0), 2)
                north["net_buy_status"] = "ok"
            else:
                north["net_buy_status"] = "unavailable"
                degraded.append(
                    {
                        "section": "northbound_net_buy",
                        "error": "kamt 北向买卖/净买入不可核对（监管披露后常为 0 或额度字段），不返回伪净流入",
                    }
                )

            # 南向：仍有买卖成交，可核对则返回
            south_parts: list[float] = []
            for key, side in (("sh2hk", "sh"), ("sz2hk", "sz")):
                row = data.get(key) or {}
                buy = self._num_or_none(row.get("buyAmt"))
                sell = self._num_or_none(row.get("sellAmt"))
                net = self._num_or_none(row.get("netBuyAmt"))
                if self._net_buy_usable(buy, sell, net):
                    south[f"{side}_net_buy_wan"] = net
                    south_parts.append(float(net))
                    if not as_of:
                        as_of = row.get("date2") or row.get("date")
            if south_parts:
                south["net_buy_wan"] = round(sum(south_parts), 2)
                south["net_buy_status"] = "ok"
            else:
                south["net_buy_status"] = "unavailable"
                degraded.append(
                    {
                        "section": "southbound_net_buy",
                        "error": "kamt 南向净买入不可核对",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            degraded.append({"section": "kamt", "error": str(exc)[:300]})

        if north.get("deal_amt_wan") is None and north.get("net_buy_wan") is None and south.get(
            "net_buy_wan"
        ) is None:
            raise MarketDataError("hsgt flow empty: " + str(degraded))

        usable = north.get("net_buy_status") == "ok"
        return {
            "as_of": as_of,
            "northbound": north,
            "southbound": south,
            "usable_for_style": usable,
            "unit": "万元",
            "degraded": degraded,
            "note": (
                "northbound.deal_amt_wan 为北向成交额（非净买入）。"
                "net_buy_status=unavailable 时不可用 day_net/额度字段做风格验证。"
                "usable_for_style 仅在北向净买入可核对时为 true。"
            ),
            "source": "eastmoney RPT_MUTUAL_DEALAMT + kamt(netBuyAmt checked)",
        }

    def _attach_share_flow_fields(
        self,
        item: dict[str, Any],
        *,
        share_info: dict[str, Any] | None,
        share_as_of: str | None,
        share_prev_as_of: str | None,
    ) -> None:
        """就地写入份额增减字段；交易所披露为 T+1，标 t1_only。"""
        if not share_info or share_info.get("share") is None:
            item["share_change_status"] = "unavailable"
            item["share"] = None
            item["prev_share"] = None
            item["share_change"] = None
            item["share_change_pct"] = None
            item["estimated_net_flow"] = None
            item["share_as_of"] = share_as_of
            item["share_prev_as_of"] = share_prev_as_of
            return
        share = share_info.get("share")
        prev_share = share_info.get("prev_share")
        share_change = share_info.get("share_change")
        share_change_pct = share_info.get("share_change_pct")
        nav = item.get("nav")
        estimated = None
        if share_change is not None and nav not in (None, 0):
            estimated = round(float(share_change) * float(nav), 2)
        item.update(
            {
                "share_change_status": "t1_only",
                "share": share,
                "prev_share": prev_share,
                "share_change": share_change,
                "share_change_pct": share_change_pct,
                "estimated_net_flow": estimated,
                "share_as_of": share_as_of,
                "share_prev_as_of": share_prev_as_of,
                "share_usable_for_intraday": False,
            }
        )

    def _etf_share_flow_bundle(
        self, codes: list[str], *, lookback_days: int = 12
    ) -> dict[str, Any]:
        """批量取交易所 T+1 份额变动；失败时 items 为空但不抛。"""
        empty = {
            "as_of": None,
            "prev_as_of": None,
            "items": {},
            "usable_for_intraday": False,
            "gaps": [],
        }
        if self.mode == "mock":
            items: dict[str, dict[str, Any]] = {}
            for i, code in enumerate(codes):
                share = 1_000_000_000.0 + i * 50_000_000.0
                prev = share * 0.995
                change = share - prev
                items[code] = {
                    "share": share,
                    "prev_share": prev,
                    "share_change": change,
                    "share_change_pct": round(change / prev * 100.0, 4),
                }
            today = date.today()
            return {
                "as_of": (today - timedelta(days=1)).isoformat(),
                "prev_as_of": (today - timedelta(days=2)).isoformat(),
                "items": items,
                "usable_for_intraday": False,
                "gaps": [],
            }
        try:
            primary, prev, sse_map = self._resolve_share_dates(
                as_of=None, lookback_days=lookback_days
            )
        except MarketDataError as exc:
            empty["gaps"].append(str(exc)[:200])
            return empty
        prev_map = sse_map.pop("__prev_map__", {}) if sse_map else {}
        if not isinstance(prev_map, dict):
            prev_map = {}
        gaps: list[str] = []
        if primary is None:
            gaps.append("无可用交易所份额披露日")
            empty["gaps"] = gaps
            return empty
        out_items: dict[str, dict[str, Any]] = {}
        for code in codes:
            exchange = self._etf_exchange(code)
            cur_row: dict[str, Any] | None = None
            prev_row: dict[str, Any] | None = None
            if exchange == "SSE":
                cur_row = sse_map.get(code)
                prev_row = prev_map.get(code)
            else:
                try:
                    cur_row = self._szse_etf_share(code, primary)
                except MarketDataError as exc:
                    gaps.append(f"{code}: {exc}")
                if prev is not None:
                    try:
                        prev_row = self._szse_etf_share(code, prev)
                    except MarketDataError as exc:
                        gaps.append(f"{code} prev: {exc}")
            if not cur_row or cur_row.get("share") is None:
                continue
            share = float(cur_row["share"])
            prev_share = float(prev_row["share"]) if prev_row and prev_row.get("share") is not None else None
            share_change = None
            share_change_pct = None
            if prev_share not in (None, 0):
                share_change = share - float(prev_share)
                share_change_pct = round(share_change / float(prev_share) * 100.0, 4)
            out_items[code] = {
                "share": share,
                "prev_share": prev_share,
                "share_change": share_change,
                "share_change_pct": share_change_pct,
            }
        return {
            "as_of": primary.isoformat(),
            "prev_as_of": prev.isoformat() if prev else None,
            "items": out_items,
            "usable_for_intraday": False,
            "gaps": gaps,
        }

    def get_etf_premium(self, codes: list[str]) -> dict[str, Any]:
        """ETF 市价相对净值溢折价（东财 FundMNFInfo）+ 交易所 T+1 份额增减。

        份额为 T+1 披露：share_change_status=t1_only，不可用于盘中当日判断。
        """
        codes = ["".join(ch for ch in str(c) if ch.isdigit())[-6:] for c in codes]
        codes = [c for c in codes if len(c) == 6][:12]
        if not codes:
            raise MarketDataError("codes required")
        share_bundle = self._etf_share_flow_bundle(codes)
        share_as_of = share_bundle.get("as_of")
        share_prev_as_of = share_bundle.get("prev_as_of")
        share_items = share_bundle.get("items") or {}
        if self.mode == "mock":
            items = []
            for code in codes:
                snap = self._mock_quote(code)
                nav = round(float(snap["price"]) * 0.995, 4)
                price = float(snap["price"])
                iopv = round(float(snap["price"]) * 0.998, 4)
                item = {
                    "code": code,
                    "name": snap["name"],
                    "price": price,
                    "nav": nav,
                    "nav_date": date.today().isoformat(),
                    "nav_is_stale": False,
                    "stale_days": 0,
                    "premium_pct": round((price - iopv) / iopv * 100, 3),
                    "premium_basis": "iopv",
                    "stale_adjusted_premium_pct": None,
                    "iopv": iopv,
                    "iopv_status": "ok",
                    "change_pct": snap.get("change_pct"),
                    "quote_time": datetime.now().isoformat(timespec="seconds"),
                }
                self._attach_share_flow_fields(
                    item,
                    share_info=share_items.get(code),
                    share_as_of=share_as_of,
                    share_prev_as_of=share_prev_as_of,
                )
                items.append(item)
            return {
                "count": len(items),
                "items": items,
                "share_as_of": share_as_of,
                "share_prev_as_of": share_prev_as_of,
                "share_usable_for_intraday": False,
                "source": "mock",
                "note": "mock：premium_pct 相对 IOPV；份额为 T+1 代理",
            }

        url = (
            "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo?"
            + urllib.parse.urlencode(
                {
                    "pageIndex": "1",
                    "pageSize": str(len(codes)),
                    "deviceid": "Wap",
                    "plat": "Wap",
                    "product": "EFund",
                    "Version": "2.0.0",
                    "Fcodes": ",".join(codes),
                }
            )
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://fund.eastmoney.com/",
            },
        )
        with opener.open(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        rows = (payload or {}).get("Datas") or []
        by_code = {str(r.get("FCODE") or "").zfill(6): r for r in rows if isinstance(r, dict)}
        # 东财 GSZ 盘后常空：腾讯行情字段 78≈IOPV 作回退
        tencent_iopv: dict[str, float] = {}
        try:
            from market_data import asd_core

            tq = asd_core.tencent_quote(codes)
            for code in codes:
                raw_iopv = (tq.get(code) or {}).get("iopv")
                num = self._num_or_none(raw_iopv)
                if num not in (None, 0):
                    tencent_iopv[code] = float(num)
        except Exception:  # noqa: BLE001
            tencent_iopv = {}
        today = date.today()
        items: list[dict[str, Any]] = []
        missing: list[str] = []
        for code in codes:
            row = by_code.get(code)
            if not row:
                missing.append(code)
                continue
            nav = self._num_or_none(row.get("NAV"))
            price = self._num_or_none(row.get("NEWPRICE"))
            # GSZ=估算净值/IOPV；盘后常为空 → 回退腾讯 iopv
            iopv = self._num_or_none(row.get("GSZ"))
            iopv_source = "eastmoney_gsz" if iopv not in (None, 0) else None
            if iopv in (None, 0) and code in tencent_iopv:
                iopv = tencent_iopv[code]
                iopv_source = "tencent_quote"
            change_pct = self._num_or_none(row.get("CHANGERATIO"))
            nav_date_raw = row.get("PDATE")
            nav_day: date | None = None
            if isinstance(nav_date_raw, str) and len(nav_date_raw) >= 10:
                try:
                    nav_day = date.fromisoformat(nav_date_raw[:10])
                except ValueError:
                    nav_day = None
            stale_days = (today - nav_day).days if nav_day else None
            nav_is_stale = bool(stale_days is not None and stale_days >= 1)

            premium = None
            premium_basis = "unavailable"
            stale_adjusted = None
            if iopv not in (None, 0) and price is not None:
                premium = round((price - iopv) / iopv * 100.0, 3)
                premium_basis = "iopv"
            elif nav not in (None, 0) and price is not None:
                premium = round((price - nav) / nav * 100.0, 3)
                premium_basis = "nav_t1"
                # 隔日净值口径下 premium ≈ 当日涨跌幅反向；剔除滞后后看残差
                if change_pct is not None:
                    stale_adjusted = round(premium - change_pct, 3)

            tip = None
            if premium_basis == "nav_t1":
                tip = (
                    "隔日净值口径（nav_is_stale=true）：premium_pct 方向常贴近当日涨跌幅反向，"
                    "不具当日折溢价参考意义；请看 stale_adjusted_premium_pct 或等 IOPV。"
                )
            elif premium_basis == "iopv" and iopv_source == "tencent_quote":
                tip = "IOPV 来自腾讯行情字段回退（东财 GSZ 为空时常用）。"
            item = {
                "code": code,
                "name": row.get("SHORTNAME") or code,
                "price": price,
                "nav": nav,
                "nav_date": nav_date_raw,
                "nav_is_stale": nav_is_stale,
                "stale_days": stale_days,
                "premium_pct": premium,
                "premium_basis": premium_basis,
                "stale_adjusted_premium_pct": stale_adjusted,
                "iopv": iopv,
                "iopv_status": "ok" if iopv is not None else "unavailable",
                "iopv_source": iopv_source,
                "quote_time": row.get("HQDATE"),
                "change_pct": change_pct,
                "tip": tip,
            }
            self._attach_share_flow_fields(
                item,
                share_info=share_items.get(code),
                share_as_of=share_as_of,
                share_prev_as_of=share_prev_as_of,
            )
            if item.get("share_change_status") == "t1_only":
                share_tip = (
                    f"份额为交易所 T+1 披露（as_of={share_as_of}），"
                    "share_change_status=t1_only，不可当作盘中当日申赎结论。"
                )
                item["tip"] = f"{tip} {share_tip}".strip() if tip else share_tip
            items.append(item)
        if not items:
            raise MarketDataError(f"etf premium empty for {codes}")
        return {
            "count": len(items),
            "items": items,
            "missing": missing,
            "share_as_of": share_as_of,
            "share_prev_as_of": share_prev_as_of,
            "share_usable_for_intraday": False,
            "share_gaps": share_bundle.get("gaps") or [],
            "note": (
                "优先 premium_basis=iopv：东财 GSZ，空则回退腾讯行情 IOPV（iopv_source 标明）；"
                "仍无 IOPV 时用已披露净值（常为 T-1）并标 nav_is_stale，"
                "stale_adjusted_premium_pct=premium_pct-change_pct 剔除当日涨跌滞后。"
                "份额来自沪深交易所 T+1 披露：share_change_status=t1_only，"
                "estimated_net_flow≈份额变动×净值（元），不可用于盘中当日判断。"
            ),
            "source": "eastmoney FundMNFInfo + tencent IOPV fallback + SSE/SZSE ETF share",
        }

    @staticmethod
    def _is_national_team_holder(name: str) -> bool:
        n = str(name or "")
        if not n:
            return False
        return any(kw in n for kw in _NT_HOLDER_KEYWORDS)

    @staticmethod
    def _normalize_etf_code(code: Any) -> str:
        digits = "".join(ch for ch in str(code or "") if ch.isdigit())
        return digits[-6:] if len(digits) >= 6 else digits

    @staticmethod
    def _etf_exchange(code: str) -> str:
        c = str(code or "")
        if c.startswith(("15", "16", "18")):
            return "SZSE"
        return "SSE"

    def _http_json_referer(
        self,
        url: str,
        *,
        referer: str,
        timeout: int = 20,
        retries: int = 2,
    ) -> Any:
        last: Exception | None = None
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
            "Accept": "application/json,text/plain,*/*",
        }
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.15 * (attempt + 1))
        raise MarketDataError(f"http json failed: {last}") from last

    def _sse_etf_shares_by_date(self, stat_date: date) -> dict[str, dict[str, Any]]:
        """上交所 ETF 份额；TOT_VOL 单位万份 → 份。"""
        url = "https://query.sse.com.cn/commonQuery.do?" + urllib.parse.urlencode(
            {
                "isPagination": "true",
                "pageHelp.pageSize": "10000",
                "pageHelp.pageNo": "1",
                "pageHelp.beginPage": "1",
                "pageHelp.cacheSize": "1",
                "pageHelp.endPage": "1",
                "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
                "STAT_DATE": stat_date.isoformat(),
            }
        )
        payload = self._http_json_referer(url, referer="https://www.sse.com.cn/", timeout=30)
        rows = payload.get("result") if isinstance(payload, dict) else None
        out: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            code = self._normalize_etf_code(row.get("SEC_CODE"))
            vol = self._num_or_none(row.get("TOT_VOL"))
            if not code or vol is None:
                continue
            out[code] = {
                "code": code,
                "name": str(row.get("SEC_NAME") or code),
                "share": vol * 10_000.0,
                "stat_date": str(row.get("STAT_DATE") or stat_date.isoformat()),
                "exchange": "SSE",
            }
        return out

    def _szse_etf_share(self, code: str, stat_date: date) -> dict[str, Any] | None:
        """深交所单只 ETF 份额；current_size 单位万份 → 份。"""
        url = "https://www.szse.cn/api/report/ShowReport/data?" + urllib.parse.urlencode(
            {
                "SHOWTYPE": "JSON",
                "CATALOGID": "scsj_fund_jjgm",
                "TABKEY": "tab1",
                "txtStart": stat_date.isoformat(),
                "txtEnd": stat_date.isoformat(),
                "jjlb": "ETF",
                "txtDm": code,
                "PAGENO": "1",
                "PAGESIZE": "5",
                "random": "0.1",
            }
        )
        payload = self._http_json_referer(
            url,
            referer="https://www.szse.cn/market/fund/volume/etf/index.html",
            timeout=20,
        )
        block = payload[0] if isinstance(payload, list) and payload else None
        rows = (block or {}).get("data") if isinstance(block, dict) else None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            row_code = self._normalize_etf_code(row.get("fund_code"))
            if row_code != code:
                continue
            raw_size = str(row.get("current_size") or "").replace(",", "")
            vol = self._num_or_none(raw_size)
            if vol is None:
                return None
            return {
                "code": code,
                "name": str(row.get("security_short_name") or code),
                "share": vol * 10_000.0,
                "stat_date": str(row.get("size_date") or stat_date.isoformat()),
                "exchange": "SZSE",
            }
        return None

    def _resolve_share_dates(
        self, *, as_of: date | None, lookback_days: int
    ) -> tuple[date | None, date | None, dict[str, dict[str, Any]]]:
        """找最近有上交所份额披露的一日，以及其前一披露日。"""
        start = as_of or (date.today() - timedelta(days=1))
        primary: date | None = None
        primary_map: dict[str, dict[str, Any]] = {}
        for i in range(max(1, lookback_days)):
            d = start - timedelta(days=i)
            try:
                m = self._sse_etf_shares_by_date(d)
            except MarketDataError:
                m = {}
            if m:
                primary = d
                primary_map = m
                break
        if primary is None:
            return None, None, {}
        prev: date | None = None
        for i in range(1, max(2, lookback_days + 1)):
            d = primary - timedelta(days=i)
            try:
                m = self._sse_etf_shares_by_date(d)
            except MarketDataError:
                m = {}
            if m:
                prev = d
                # stash prev map under a private key for reuse
                primary_map["__prev_map__"] = m  # type: ignore[assignment]
                break
        return primary, prev, primary_map

    def _sina_holder_report_dates(self, code: str) -> list[str]:
        url = f"https://stock.finance.sina.com.cn/fundInfo/view/FundInfo_JJCYR.php?symbol={code}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            },
        )
        try:
            with opener.open(req, timeout=15) as resp:
                text = resp.read().decode("gbk", errors="replace")
        except Exception:  # noqa: BLE001
            return []
        return re.findall(
            r'<option[^>]*value=["\'](\d{4}-\d{2}-\d{2})["\']',
            text,
            flags=re.IGNORECASE,
        )

    def _sina_fund_holders(self, code: str, report_date: str) -> list[dict[str, Any]]:
        url = (
            "https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/"
            "CaihuiFundInfoService.getFundHolder?"
            + urllib.parse.urlencode({"symbol": code, "date": report_date})
        )
        payload = self._http_json_referer(
            url,
            referer=(
                "https://stock.finance.sina.com.cn/fundInfo/view/"
                f"FundInfo_JJCYR.php?symbol={code}"
            ),
            timeout=15,
        )
        rows = ((payload or {}).get("result") or {}).get("data") or []
        out: list[dict[str, Any]] = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            name = str(row.get("cyrmc") or "").strip()
            if not name:
                continue
            amount = self._num_or_none(row.get("cyfe"))
            ratio = self._num_or_none(row.get("zfeb"))
            out.append(
                {
                    "rank": i + 1,
                    "name": name,
                    "hold_amount": amount,
                    "hold_ratio": ratio,
                    "is_nt": self._is_national_team_holder(name),
                }
            )
        return out

    def _fetch_nav_map(self, codes: list[str]) -> dict[str, float | None]:
        if not codes:
            return {}
        if self.mode == "mock":
            return {c: float(self._mock_quote(c)["price"]) * 0.995 for c in codes}
        url = (
            "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo?"
            + urllib.parse.urlencode(
                {
                    "pageIndex": "1",
                    "pageSize": str(len(codes)),
                    "deviceid": "Wap",
                    "plat": "Wap",
                    "product": "EFund",
                    "Version": "2.0.0",
                    "Fcodes": ",".join(codes),
                }
            )
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"},
        )
        try:
            with opener.open(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {c: None for c in codes}
        rows = (payload or {}).get("Datas") or []
        by_code = {str(r.get("FCODE") or "").zfill(6): r for r in rows if isinstance(r, dict)}
        return {c: self._num_or_none((by_code.get(c) or {}).get("NAV")) for c in codes}

    def get_national_team_flow(
        self,
        *,
        codes: list[str] | None = None,
        as_of: str | date | None = None,
        include_holders: bool = True,
        lookback_days: int = 12,
    ) -> dict[str, Any]:
        """国家队相关宽基 ETF 份额变动代理（非实时可归因买入）。

        - 份额：上交所/深交所 T+1 披露；份额增减 ≠ 确认为汇金/证金下单
        - 持有人：新浪定期报告十大持有人，最长滞后约半年；用关键词标记国家队席位
        """
        seed_by_code = {x["code"]: x for x in _NT_SEED_ETFS}
        if codes:
            norm = [self._normalize_etf_code(c) for c in codes]
            selected = [c for c in norm if len(c) == 6][:12]
        else:
            selected = [x["code"] for x in _NT_SEED_ETFS]
        if not selected:
            raise MarketDataError("codes required")

        as_of_day: date | None = None
        if isinstance(as_of, date):
            as_of_day = as_of
        elif isinstance(as_of, str) and as_of.strip():
            as_of_day = date.fromisoformat(as_of.strip()[:10])

        holder_stale_days_threshold = 180
        if self.mode == "mock":
            items = []
            flow_sum = 0.0
            holders_as_of = "2025-12-31"
            stale_days = (date.today() - date.fromisoformat(holders_as_of)).days
            holder_status = "stale" if stale_days > holder_stale_days_threshold else "ok"
            for i, code in enumerate(selected):
                share = 2_000_000_000.0 + i * 100_000_000.0
                prev = share * 0.99
                change = share - prev
                nav = 4.0 + i * 0.1
                flow = round(change * nav, 2)
                flow_sum += flow
                name = (seed_by_code.get(code) or {}).get("name") or self._mock_quote(code)["name"]
                items.append(
                    {
                        "code": code,
                        "name": name,
                        "exchange": self._etf_exchange(code),
                        "share": share,
                        "prev_share": prev,
                        "share_change": change,
                        "share_change_pct": round(change / prev * 100.0, 4),
                        "nav": nav,
                        "estimated_net_flow": flow,
                        "flow_status": "ok",
                        "holders_as_of": holders_as_of,
                        "holder_report_period": holders_as_of,
                        "staleness_days": stale_days,
                        "nt_holders": [
                            {
                                "rank": 1,
                                "name": "中央汇金投资有限责任公司",
                                "hold_amount": share * 0.4,
                                "hold_ratio": 40.0,
                                "is_nt": True,
                            }
                        ],
                        "nt_hold_ratio_sum": 40.0,
                        "holder_status": holder_status,
                        "holder_tip": (
                            f"持有人报告期滞后 {stale_days} 天（阈值 {holder_stale_days_threshold}），"
                            "无最新定期报告，勿当作近期加减仓证据。"
                            if holder_status == "stale"
                            else None
                        ),
                    }
                )
            return {
                "as_of": (as_of_day or date.today()).isoformat(),
                "prev_as_of": ((as_of_day or date.today()) - timedelta(days=1)).isoformat(),
                "attribution": "proxy",
                "usable_as_realtime_nt_orders": False,
                "holder_stale_days_threshold": holder_stale_days_threshold,
                "count": len(items),
                "items": items,
                "summary": {
                    "estimated_net_flow_sum": round(flow_sum, 2),
                    "share_up": len(items),
                    "share_down": 0,
                    "share_flat": 0,
                },
                "gaps": [],
                "note": (
                    "mock：份额增减仅为代理信号；不可当作国家队实时买卖。"
                    "持有人来自定期报告快照；滞后超阈值标 holder_status=stale。"
                ),
                "source": "mock",
            }

        primary, prev, sse_map = self._resolve_share_dates(
            as_of=as_of_day, lookback_days=max(3, int(lookback_days or 12))
        )
        prev_map = sse_map.pop("__prev_map__", {}) if sse_map else {}
        gaps: list[str] = []
        if primary is None:
            gaps.append("上交所近期无可用 ETF 份额披露日")
        if primary and prev is None:
            gaps.append("缺少上一披露日，无法计算份额变动")

        nav_map = self._fetch_nav_map(selected)
        items: list[dict[str, Any]] = []
        flow_sum = 0.0
        flow_usable = 0
        up = down = flat = 0

        for code in selected:
            exchange = self._etf_exchange(code)
            name = (seed_by_code.get(code) or {}).get("name") or code
            cur_row: dict[str, Any] | None = None
            prev_row: dict[str, Any] | None = None
            if exchange == "SSE":
                cur_row = sse_map.get(code)
                prev_row = prev_map.get(code) if isinstance(prev_map, dict) else None
            else:
                if primary is not None:
                    try:
                        cur_row = self._szse_etf_share(code, primary)
                    except MarketDataError as exc:
                        gaps.append(f"{code} 深交所份额失败: {exc}")
                if prev is not None:
                    try:
                        prev_row = self._szse_etf_share(code, prev)
                    except MarketDataError as exc:
                        gaps.append(f"{code} 深交所昨份额失败: {exc}")

            if cur_row:
                name = str(cur_row.get("name") or name)
            share = (cur_row or {}).get("share")
            prev_share = (prev_row or {}).get("share")
            share_change = None
            share_change_pct = None
            estimated_flow = None
            flow_status = "unavailable"
            nav = nav_map.get(code)
            if share is not None and prev_share not in (None, 0):
                share_change = float(share) - float(prev_share)
                share_change_pct = round(share_change / float(prev_share) * 100.0, 4)
                if nav is not None:
                    estimated_flow = round(share_change * float(nav), 2)
                    flow_status = "ok"
                    flow_sum += estimated_flow
                    flow_usable += 1
                if share_change > 0:
                    up += 1
                elif share_change < 0:
                    down += 1
                else:
                    flat += 1
            elif share is None:
                gaps.append(f"{code} 无份额数据")

            nt_holders: list[dict[str, Any]] = []
            holders_as_of: str | None = None
            holder_status = "skipped"
            staleness_days: int | None = None
            holder_tip: str | None = None
            if include_holders:
                holder_status = "unavailable"
                report_dates = self._sina_holder_report_dates(code)
                for rd in report_dates[:3]:
                    try:
                        holders = self._sina_fund_holders(code, rd)
                    except MarketDataError:
                        holders = []
                    nt = [h for h in holders if h.get("is_nt")]
                    if nt:
                        nt_holders = nt
                        holders_as_of = rd
                        holder_status = "ok"
                        break
                if holder_status != "ok" and report_dates:
                    # 无国家队席位也返回空列表并标明报告期
                    try:
                        all_h = self._sina_fund_holders(code, report_dates[0])
                    except MarketDataError:
                        all_h = []
                    if all_h:
                        holders_as_of = report_dates[0]
                        holder_status = "ok_no_nt"
                        nt_holders = []
                if holders_as_of:
                    try:
                        report_day = date.fromisoformat(str(holders_as_of)[:10])
                        staleness_days = (date.today() - report_day).days
                        if staleness_days > holder_stale_days_threshold:
                            holder_status = "stale"
                            holder_tip = (
                                f"持有人报告期 {holders_as_of} 滞后 {staleness_days} 天"
                                f"（阈值 {holder_stale_days_threshold}），无最新定期报告，"
                                "勿当作近期加减仓证据。"
                            )
                    except ValueError:
                        staleness_days = None

            nt_ratio_sum = None
            if nt_holders:
                nt_ratio_sum = round(
                    sum(float(h["hold_ratio"]) for h in nt_holders if h.get("hold_ratio") is not None),
                    4,
                )

            items.append(
                {
                    "code": code,
                    "name": name,
                    "exchange": exchange,
                    "share": share,
                    "prev_share": prev_share,
                    "share_change": share_change,
                    "share_change_pct": share_change_pct,
                    "nav": nav,
                    "estimated_net_flow": estimated_flow,
                    "flow_status": flow_status,
                    "holders_as_of": holders_as_of,
                    "holder_report_period": holders_as_of,
                    "staleness_days": staleness_days,
                    "nt_holders": nt_holders,
                    "nt_hold_ratio_sum": nt_ratio_sum,
                    "holder_status": holder_status,
                    "holder_tip": holder_tip,
                }
            )

        return {
            "as_of": primary.isoformat() if primary else None,
            "prev_as_of": prev.isoformat() if prev else None,
            "attribution": "proxy",
            "usable_as_realtime_nt_orders": False,
            "holder_stale_days_threshold": holder_stale_days_threshold,
            "count": len(items),
            "items": items,
            "summary": {
                "estimated_net_flow_sum": round(flow_sum, 2) if flow_usable else None,
                "share_up": up,
                "share_down": down,
                "share_flat": flat,
                "flow_ok_count": flow_usable,
            },
            "gaps": gaps,
            "note": (
                "attribution=proxy：交易所 T+1 份额增减是申赎总量代理，"
                "不能归因到汇金/证金/社保的实时买卖。"
                "nt_holders 来自新浪定期报告十大持有人；"
                f"holders_as_of 滞后超过 {holder_stale_days_threshold} 天时 holder_status=stale。"
                "estimated_net_flow≈份额变动×净值，单位元。"
            ),
            "source": "SSE/SZSE ETF share + sina CaihuiFundInfoService.getFundHolder",
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

    def get_market_news(
        self,
        *,
        scope: str = "market",
        codes: list[str] | None = None,
        limit: int = 20,
        sources: list[str] | None = None,
        max_chars: int = 300,
    ) -> dict[str, Any]:
        """聚合盘中资讯：财联社电报 + 华尔街见闻快讯 + 可选东财个股新闻。"""
        limit = max(1, min(int(limit), 50))
        max_chars = max(80, min(int(max_chars), 800))
        scope = (scope or "market").lower()
        if scope not in ("market", "stock", "both"):
            scope = "market"
        wanted = {s.lower() for s in (sources or ["cls", "wscn", "eastmoney"])}
        codes = [c for c in (codes or []) if c]
        if self.mode == "mock":
            items = self._mock_market_news(scope=scope, codes=codes, limit=limit)
            return {
                "scope": scope,
                "count": len(items),
                "items": items,
                "sources_used": sorted(wanted),
                "source": "mock",
            }

        from market_data import news as news_mod

        items: list[dict[str, Any]] = []
        degraded: list[dict[str, str]] = []
        sources_used: list[str] = []

        if scope in ("market", "both"):
            if "cls" in wanted:
                try:
                    rows = news_mod.fetch_cls_telegraph(
                        limit=min(limit, 40), max_chars=max_chars
                    )
                    items.extend(rows)
                    sources_used.append("cls")
                except Exception as exc:  # noqa: BLE001
                    degraded.append({"section": "cls", "error": str(exc)[:300]})
            if "wscn" in wanted:
                try:
                    rows = news_mod.fetch_wscn_lives(
                        channel="a-stock-channel",
                        limit=min(limit, 40),
                        max_chars=max_chars,
                    )
                    items.extend(rows)
                    sources_used.append("wscn")
                except Exception as exc:  # noqa: BLE001
                    degraded.append({"section": "wscn", "error": str(exc)[:300]})

        if scope in ("stock", "both") and codes:
            if "eastmoney" in wanted:
                per = max(3, min(10, limit // max(len(codes), 1)))
                for code in codes[:8]:
                    try:
                        rows = news_mod.fetch_eastmoney_stock_news(
                            code, limit=per, max_chars=max_chars
                        )
                        items.extend(rows)
                        if "eastmoney" not in sources_used:
                            sources_used.append("eastmoney")
                    except Exception as exc:  # noqa: BLE001
                        degraded.append(
                            {"section": f"eastmoney:{code}", "error": str(exc)[:300]}
                        )

        # 按时间倒序去重（title+source）
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for it in sorted(items, key=lambda r: r.get("time") or "", reverse=True):
            key = f"{it.get('source')}|{it.get('title')}"
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
            if len(uniq) >= limit:
                break

        if not uniq and degraded:
            raise MarketDataError("market news empty: " + str(degraded))
        return {
            "scope": scope,
            "count": len(uniq),
            "items": uniq,
            "sources_used": sources_used,
            "degraded": degraded,
            "source": "cls+wscn+eastmoney",
        }

    def get_announcements(
        self,
        *,
        codes: list[str] | None = None,
        days: int = 30,
        limit: int = 20,
        max_chars: int = 200,
    ) -> dict[str, Any]:
        """巨潮资讯公告摘要（按代码聚合，截断标题/类型）。"""
        codes = ["".join(ch for ch in str(c) if ch.isdigit())[-6:] for c in (codes or [])]
        codes = [c for c in codes if len(c) == 6][:8]
        if not codes:
            raise MarketDataError("get_announcements 需要至少一个 6 位代码")
        limit = max(1, min(int(limit), 50))
        days = max(1, min(int(days), 180))
        max_chars = max(60, min(int(max_chars), 400))
        if self.mode == "mock":
            items = self._mock_announcements(codes=codes, limit=limit)
            return {
                "codes": codes,
                "days": days,
                "count": len(items),
                "items": items,
                "source": "mock",
            }

        from market_data import news as news_mod

        cutoff = (date.today() - timedelta(days=days)).isoformat()
        items: list[dict[str, Any]] = []
        degraded: list[dict[str, str]] = []
        per = max(5, min(20, limit // max(len(codes), 1) + 3))
        for code in codes:
            try:
                rows = news_mod.fetch_cninfo_announcements(
                    code, limit=per, max_chars=max_chars
                )
                if not rows:
                    degraded.append(
                        {
                            "section": f"cninfo:{code}",
                            "error": "巨潮无 orgId 映射或无公告（沪市部分 ETF 常见），未编造结果",
                        }
                    )
                items.extend(r for r in rows if (r.get("date") or "") >= cutoff)
            except Exception as exc:  # noqa: BLE001
                degraded.append({"section": f"cninfo:{code}", "error": str(exc)[:300]})

        items = sorted(items, key=lambda r: r.get("date") or "", reverse=True)[:limit]
        if not items and degraded and not any(
            d.get("error", "").startswith("巨潮无 orgId") for d in degraded
        ):
            raise MarketDataError("announcements empty: " + str(degraded))
        return {
            "codes": codes,
            "days": days,
            "count": len(items),
            "items": items,
            "degraded": degraded,
            "source": "cninfo",
            "note": "无映射/无公告时 items 可为空并写入 degraded，不会返回假公告",
        }

    def get_macro_digest(
        self,
        *,
        days_ahead: int = 7,
        days_back: int = 1,
        country: str | None = None,
        min_importance: int = 2,
        include_cctv: bool = True,
        max_chars: int = 300,
    ) -> dict[str, Any]:
        """宏观日历（华尔街见闻）+ 可选新闻联播标题，供盘后/中长期判断。"""
        days_ahead = max(0, min(int(days_ahead), 30))
        days_back = max(0, min(int(days_back), 14))
        min_importance = max(1, min(int(min_importance), 4))
        max_chars = max(80, min(int(max_chars), 600))
        today = date.today()
        start = today - timedelta(days=days_back)
        end = today + timedelta(days=days_ahead)
        if self.mode == "mock":
            return {
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "calendar": self._mock_macro_calendar(start, end, min_importance),
                "cctv": self._mock_cctv(today - timedelta(days=1)) if include_cctv else [],
                "source": "mock",
            }

        from market_data import news as news_mod

        degraded: list[dict[str, str]] = []
        calendar: list[dict[str, Any]] = []
        try:
            calendar = news_mod.fetch_macro_calendar(
                start,
                end,
                country=country,
                min_importance=min_importance,
            )
        except Exception as exc:  # noqa: BLE001
            degraded.append({"section": "macro_calendar", "error": str(exc)[:300]})

        cctv: list[dict[str, Any]] = []
        if include_cctv:
            # 优先昨日联播；若空再试前日
            for offset in (1, 2, 0):
                day = today - timedelta(days=offset)
                try:
                    cctv = news_mod.fetch_cctv_news(
                        day, with_content=False, max_chars=max_chars
                    )
                    if cctv:
                        break
                except Exception as exc:  # noqa: BLE001
                    degraded.append(
                        {"section": f"cctv:{day.isoformat()}", "error": str(exc)[:300]}
                    )

        if not calendar and not cctv and degraded:
            raise MarketDataError("macro digest empty: " + str(degraded))
        return {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "calendar": calendar,
            "cctv": cctv,
            "degraded": degraded,
            "source": "wscn_macro+cctv",
        }

    def _mock_market_news(
        self, *, scope: str, codes: list[str], limit: int
    ) -> list[dict[str, Any]]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        items: list[dict[str, Any]] = []
        if scope in ("market", "both"):
            items.extend(
                [
                    {
                        "time": now,
                        "title": "两市成交额回升，科技板块领涨",
                        "summary": "模拟快讯：沪指震荡上行，半导体与软件走强。",
                        "source": "财联社",
                        "url": "",
                    },
                    {
                        "time": now,
                        "title": "北向资金午后净流入扩大",
                        "summary": "模拟快讯：外资回流宽基 ETF。",
                        "source": "华尔街见闻",
                        "url": "",
                    },
                ]
            )
        if scope in ("stock", "both"):
            for code in (codes or ["510300"])[:3]:
                items.append(
                    {
                        "time": now,
                        "title": f"{code} 相关机构点评",
                        "summary": f"模拟个股新闻：关注 {code} 流动性与溢价。",
                        "source": "东财",
                        "url": "",
                        "code": code,
                    }
                )
        return items[:limit]

    def _mock_announcements(
        self, *, codes: list[str], limit: int
    ) -> list[dict[str, Any]]:
        today = date.today().isoformat()
        items: list[dict[str, Any]] = []
        for code in codes:
            items.append(
                {
                    "date": today,
                    "title": f"{code} 关于基金份额折算的公告",
                    "type": "其他",
                    "summary": f"其他 {code} 关于基金份额折算的公告",
                    "source": "巨潮",
                    "url": "",
                    "code": code,
                }
            )
        return items[:limit]

    def _mock_macro_calendar(
        self, start: date, end: date, min_importance: int
    ) -> list[dict[str, Any]]:
        mid = start + timedelta(days=min(2, (end - start).days))
        past = start
        return [
            {
                "time": f"{past.isoformat()} 09:30",
                "country": "中国",
                "title": "制造业 PMI",
                "kind": "data",
                "importance": max(min_importance, 3),
                "actual": "50.1",
                "actual_status": "released",
                "forecast": "50.2",
                "previous": "50.0",
                "unit": "",
            },
            {
                "time": f"{(mid + timedelta(days=1)).isoformat()} 21:30",
                "country": "美国",
                "title": "非农就业人数",
                "kind": "data",
                "importance": 4,
                "actual": None,
                "actual_status": "pending",
                "forecast": "18万",
                "previous": "15万",
                "unit": "",
            },
        ]

    def _mock_cctv(self, day: date) -> list[dict[str, Any]]:
        return [
            {
                "date": day.isoformat(),
                "title": "中央政治局召开会议研究经济工作",
                "summary": "中央政治局召开会议研究经济工作",
                "source": "新闻联播",
                "url": "",
            },
            {
                "date": day.isoformat(),
                "title": "多部门部署稳外贸稳外资措施",
                "summary": "多部门部署稳外贸稳外资措施",
                "source": "新闻联播",
                "url": "",
            },
        ]

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