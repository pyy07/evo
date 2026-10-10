"""资讯取数（改编自 simonlin1212/a-stock-data，Apache-2.0）。

聚合为 LLM 友好的短摘要列表，避免把全文/PDF 塞进上下文。
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_CN_TZ = timezone(timedelta(hours=8))
_EM_LAST = 0.0
_EM_MIN_INTERVAL = 0.35

WSCN_LIVES_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
WSCN_MACRO_URL = "https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas"
CCTV_DAY_URL = "https://tv.cctv.com/lm/xwlb/day/{ymd}.shtml"

_CNINFO_ORGID_MAP: dict[str, str] = {}


class NewsFetchError(Exception):
    pass


def _request(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15,
    eastmoney: bool = False,
) -> tuple[bytes, str]:
    global _EM_LAST
    if eastmoney:
        gap = _EM_MIN_INTERVAL - (time.time() - _EM_LAST)
        if gap > 0:
            time.sleep(gap)
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    body: bytes | None = None
    if data is not None:
        if isinstance(data, bytes):
            body = data
        else:
            body = urllib.parse.urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        raise NewsFetchError(f"HTTP {exc.code} {url}") from exc
    except Exception as exc:  # noqa: BLE001
        raise NewsFetchError(f"请求失败 {url}: {exc}") from exc
    if eastmoney:
        _EM_LAST = time.time()
    return raw, final_url


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _clip(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _norm_item(
    *,
    time_s: str,
    title: str,
    summary: str,
    source: str,
    url: str = "",
    extra: dict[str, Any] | None = None,
    max_chars: int = 300,
) -> dict[str, Any]:
    item = {
        "time": time_s or "",
        "title": _strip_html(title)[:200],
        "summary": _clip(_strip_html(summary), max_chars),
        "source": source,
        "url": url or "",
    }
    if extra:
        item.update(extra)
    return item


def fetch_cls_telegraph(limit: int = 50, *, max_chars: int = 300) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    params = {
        "appName": "CailianpressWeb",
        "os": "web",
        "sv": "7.7.5",
        "last_time": "",
        "refresh_type": "1",
        "rn": str(limit),
    }
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
    raw, _ = _request(url, headers={"Referer": "https://www.cls.cn/"}, timeout=12)
    payload = json.loads(raw.decode("utf-8", "replace"))
    rows: list[dict[str, Any]] = []
    for item in (payload.get("data") or {}).get("roll_data") or []:
        ts = item.get("ctime")
        t = (
            datetime.fromtimestamp(ts, _CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            if ts
            else ""
        )
        title = item.get("title") or item.get("brief") or ""
        content = item.get("content") or item.get("brief") or ""
        rows.append(
            _norm_item(
                time_s=t,
                title=title,
                summary=content,
                source="财联社",
                max_chars=max_chars,
            )
        )
    return rows


def fetch_wscn_lives(
    channel: str = "a-stock-channel",
    limit: int = 50,
    *,
    max_chars: int = 300,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    if not re.fullmatch(r"[a-z0-9-]+-channel", str(channel)):
        channel = "a-stock-channel"
    raw, _ = _request(
        WSCN_LIVES_URL,
        params={"channel": channel, "limit": limit},
        timeout=15,
    )
    payload = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(payload, dict) or payload.get("code") != 20000:
        raise NewsFetchError(f"华尔街见闻错误: {str(payload)[:160]}")
    items = (payload.get("data") or {}).get("items") or []
    rows: list[dict[str, Any]] = []
    for item in items:
        stamp = item.get("display_time")
        if not isinstance(stamp, (int, float)):
            continue
        rows.append(
            _norm_item(
                time_s=datetime.fromtimestamp(stamp, _CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                title=item.get("title") or "",
                summary=(item.get("content_text") or "").strip(),
                source="华尔街见闻",
                url=item.get("uri") or "",
                extra={"importance": item.get("score")},
                max_chars=max_chars,
            )
        )
    return rows


def fetch_eastmoney_stock_news(
    code: str, limit: int = 20, *, max_chars: int = 300
) -> list[dict[str, Any]]:
    code = "".join(ch for ch in code if ch.isdigit())[-6:]
    if len(code) != 6:
        raise NewsFetchError(f"无效代码: {code}")
    limit = max(1, min(int(limit), 50))
    cb = "jQuery_news"
    inner = json.dumps(
        {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": limit,
                    "preTag": "",
                    "postTag": "",
                }
            },
        },
        separators=(",", ":"),
    )
    raw, _ = _request(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": cb, "param": inner},
        headers={"Referer": "https://so.eastmoney.com/"},
        timeout=15,
        eastmoney=True,
    )
    text = raw.decode("utf-8", "replace")
    try:
        json_str = text[text.index("(") + 1 : text.rindex(")")]
        data = json.loads(json_str)
    except Exception as exc:  # noqa: BLE001
        raise NewsFetchError(f"东财新闻 JSONP 解析失败: {exc}") from exc
    articles = (data.get("result") or {}).get("cmsArticleWebOld") or []
    rows: list[dict[str, Any]] = []
    for a in articles:
        rows.append(
            _norm_item(
                time_s=str(a.get("date") or ""),
                title=a.get("title") or "",
                summary=a.get("content") or "",
                source=a.get("mediaName") or "东财",
                url=a.get("url") or "",
                extra={"code": code},
                max_chars=max_chars,
            )
        )
    return rows


def _cninfo_orgid(code: str) -> str:
    global _CNINFO_ORGID_MAP
    code = "".join(ch for ch in code if ch.isdigit())[-6:]
    if not _CNINFO_ORGID_MAP:
        try:
            raw, _ = _request(
                "http://www.cninfo.com.cn/new/data/szse_stock.json",
                timeout=15,
            )
            stock_list = json.loads(raw.decode("utf-8", "replace")).get("stockList") or []
            _CNINFO_ORGID_MAP = {
                s["code"]: s["orgId"] for s in stock_list if s.get("code") and s.get("orgId")
            }
        except Exception:  # noqa: BLE001
            _CNINFO_ORGID_MAP = {}
    if code in _CNINFO_ORGID_MAP:
        return _CNINFO_ORGID_MAP[code]
    # fallback prefix heuristic
    if code.startswith(("5", "6", "9")):
        prefix = "sh"
    else:
        prefix = "sz"
    return f"gs{prefix}0{code}"


def fetch_cninfo_announcements(
    code: str, limit: int = 30, *, max_chars: int = 200
) -> list[dict[str, Any]]:
    code = "".join(ch for ch in code if ch.isdigit())[-6:]
    if len(code) != 6:
        raise NewsFetchError(f"无效代码: {code}")
    limit = max(1, min(int(limit), 50))
    org_id = _cninfo_orgid(code)
    raw, _ = _request(
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        method="POST",
        data={
            "stock": f"{code},{org_id}",
            "tabName": "fulltext",
            "pageSize": str(limit),
            "pageNum": "1",
            "column": "",
            "category": "",
            "plate": "",
            "seDate": "",
            "searchkey": "",
            "secid": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        },
        headers={
            "Referer": "https://www.cninfo.com.cn/new/disclosure",
            "Origin": "https://www.cninfo.com.cn",
        },
        timeout=15,
    )
    payload = json.loads(raw.decode("utf-8", "replace"))
    rows: list[dict[str, Any]] = []
    for item in payload.get("announcements") or []:
        ts = item.get("announcementTime")
        if isinstance(ts, (int, float)):
            d = datetime.fromtimestamp(ts / 1000, _CN_TZ).strftime("%Y-%m-%d")
        else:
            d = str(ts)[:10] if ts else ""
        title = _strip_html(item.get("announcementTitle") or "")
        ann_type = item.get("announcementTypeName") or ""
        rows.append(
            {
                "date": d,
                "title": title[:200],
                "type": ann_type,
                "summary": _clip(f"{ann_type} {title}".strip(), max_chars),
                "source": "巨潮",
                "url": (
                    "https://www.cninfo.com.cn/new/disclosure/detail?"
                    f"annoId={item.get('announcementId', '')}"
                ),
                "code": code,
            }
        )
    return rows


def fetch_macro_calendar(
    start: date,
    end: date,
    *,
    country: str | None = None,
    min_importance: int = 2,
) -> list[dict[str, Any]]:
    if start > end or (end - start).days > 91:
        raise NewsFetchError("宏观日历区间无效（start≤end 且≤92天）")
    min_importance = max(1, min(int(min_importance), 4))
    rows: dict[Any, dict[str, Any]] = {}
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=6), end)
        begin = int(datetime(cursor.year, cursor.month, cursor.day, tzinfo=_CN_TZ).timestamp())
        finish = int(
            datetime(stop.year, stop.month, stop.day, 23, 59, 59, tzinfo=_CN_TZ).timestamp()
        )
        raw, _ = _request(
            WSCN_MACRO_URL,
            params={"start": begin, "end": finish},
            timeout=15,
        )
        payload = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(payload, dict) or payload.get("code") != 20000:
            raise NewsFetchError(f"宏观日历错误: {str(payload)[:160]}")
        items = (payload.get("data") or {}).get("items") or []
        for item in items:
            stamp = item.get("public_date")
            if not isinstance(stamp, (int, float)):
                continue
            when = datetime.fromtimestamp(stamp, _CN_TZ)
            level = item.get("importance")
            if not isinstance(level, int) or level < min_importance:
                continue
            if country and item.get("country") != country:
                continue
            rows[item.get("id") or f"{stamp}-{item.get('title')}"] = {
                "time": when.strftime("%Y-%m-%d %H:%M"),
                "country": item.get("country"),
                "title": item.get("title"),
                "kind": {"FD": "data", "FE": "event"}.get(
                    item.get("calendar_type"), item.get("calendar_type")
                ),
                "importance": level,
                "actual": item.get("actual") if item.get("actual") not in ("", None) else None,
                "forecast": item.get("forecast")
                if item.get("forecast") not in ("", None)
                else None,
                "previous": item.get("previous")
                if item.get("previous") not in ("", None)
                else None,
                "unit": item.get("unit") or None,
            }
        cursor = stop + timedelta(days=1)
    return sorted(rows.values(), key=lambda r: r["time"])


def fetch_cctv_news(day: date, *, with_content: bool = False, max_chars: int = 400) -> list[dict[str, Any]]:
    ymd = day.strftime("%Y%m%d")
    url = CCTV_DAY_URL.format(ymd=ymd)
    try:
        raw, _ = _request(url, timeout=15)
    except NewsFetchError as exc:
        if "404" in str(exc):
            return []
        raise
    text = raw.decode("utf-8", "replace")
    rows: list[dict[str, Any]] = []
    for chunk in text.split("<li")[1:]:
        link = re.search(r'href="([^"]*/VIDE[^"]+)"', chunk)
        if not link:
            continue
        href = link.group(1)
        title_m = (
            re.search(r'title="([^"]+)"', chunk)
            or re.search(r'class="title">(.*?)</div>', chunk, re.S)
            or re.search(r"<a[^>]*>(.*?)</a>", chunk, re.S)
        )
        title = _html.unescape(_strip_html(title_m.group(1))).strip() if title_m else ""
        if not title or re.match(r"《新闻联播》\s*\d{8}|新闻联播完整版", title):
            continue
        title = re.sub(r"^\[视频\]", "", title).strip()
        full_url = ("https:" + href) if href.startswith("//") else href
        item = {
            "date": day.isoformat(),
            "title": title[:200],
            "summary": title[:max_chars],
            "source": "新闻联播",
            "url": full_url,
        }
        if with_content:
            try:
                body_raw, _ = _request(full_url, timeout=12)
                body_text = body_raw.decode("utf-8", "replace")
                match = re.search(
                    r'<div class="content_area"[^>]*>(.*?)</div>', body_text, re.S
                ) or re.search(r'<div class="cnt_bd"[^>]*>(.*?)</div>', body_text, re.S)
                if match:
                    body = re.sub(r"</p>|<br\s*/?>", "\n", match.group(1))
                    body = _html.unescape(_strip_html(body))
                    body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
                    item["summary"] = _clip(body, max_chars)
                time.sleep(0.15)
            except Exception:  # noqa: BLE001
                pass
        rows.append(item)
    return rows
