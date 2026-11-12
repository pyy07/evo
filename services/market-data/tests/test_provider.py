from market_data.provider import MarketDataProvider


def test_mock_screen_ranks_universe():
    p = MarketDataProvider(mode="mock")
    members = p.list_universe(stock_type="stock", index_code="000300")
    assert len(members) >= 20
    screen = p.screen_market(stock_type="stock", index_code="000300", top_n=10)
    assert screen["stock_type"] == "stock"
    assert screen["universe_size"] == len(members)
    assert len(screen["candidates"]) == 10
    assert "code" in screen["candidates"][0]
    assert "change_pct" in screen["candidates"][0]


def test_mock_screen_by_stock_type():
    p = MarketDataProvider(mode="mock")
    etf = p.screen_market(stock_type="etf", top_n=8)
    assert etf["stock_type"] == "etf"
    assert any(c["code"].startswith("51") for c in etf["candidates"])
    cb = p.screen_market(stock_type="convertible_bond", top_n=5)
    assert cb["stock_type"] == "convertible_bond"
    assert all(c["code"].startswith(("11", "12")) for c in cb["candidates"])


def test_mock_snapshot_and_history():
    p = MarketDataProvider(mode="mock")
    quotes = p.get_market_snapshot(["510300", "511010"])
    assert "510300" in quotes
    assert quotes["510300"]["price"] > 0
    bars = p.get_etf_history("510300", count=10)
    assert len(bars) > 0
    assert bars[-1]["date"] >= bars[0]["date"]
    cal = p.get_trading_calendar(2026, 3)
    assert len(cal) >= 28


def test_mock_history_is_latest_n():
    p = MarketDataProvider(mode="mock")
    bars = p.get_etf_history("510300", count=40)
    assert 1 <= len(bars) <= 40
    assert "open" in bars[-1] and "close" in bars[-1]
    assert bars[-1]["date"] == max(b["date"] for b in bars)


def test_mock_market_overview():
    p = MarketDataProvider(mode="mock")
    overview = p.get_market_overview(top_n=3)
    assert "indices" in overview
    assert overview["industry"]["leaders"]
    assert overview["concept"]["hot"]
    assert overview["industry_fund_flow"]["inflow"]
    assert "breadth_proxy" in overview
    assert overview["breadth"]["histogram"]
    assert overview["index_valuation"]["pe"]
    rank = p.industry_comparison(top_n=3, board="industry")
    assert rank["top"]
    flow = p.board_fund_flow(board_type="industry", period="today", top_n=3)
    assert flow["inflow"]


def test_mock_news_aggregators():
    p = MarketDataProvider(mode="mock")
    news = p.get_market_news(scope="both", codes=["510300"], limit=10)
    assert news["count"] >= 2
    assert news["items"][0]["title"]
    anns = p.get_announcements(codes=["510300", "000001"], limit=5)
    assert anns["count"] >= 1
    assert anns["items"][0]["code"] in ("510300", "000001")
    macro = p.get_macro_digest(days_ahead=5, include_cctv=True)
    assert macro["calendar"]
    assert macro["cctv"]
    assert "window" in macro


def test_overview_degrades_when_industry_fails():
    p = MarketDataProvider(mode="mock")

    def boom(**_kwargs):
        raise RuntimeError("Remote end closed connection without response")

    p.industry_comparison = boom  # type: ignore[method-assign]
    overview = p.get_market_overview(top_n=3)
    assert overview["indices"] or overview["degraded"]
    assert any(d["section"] == "industry_comparison" for d in overview["degraded"])
    assert overview["industry"]["leaders"] == []


def test_industry_falls_back_when_eastmoney_fails():
    p = MarketDataProvider(mode="live")

    def boom(**_kwargs):
        raise RuntimeError("Remote end closed connection without response")

    p._eastmoney_clist = boom  # type: ignore[method-assign]
    p._sina_hy_boards = lambda: [  # type: ignore[method-assign]
        {
            "code": "new_dxhy",
            "name": "半导体",
            "change_pct": 2.1,
            "amount": 1e10,
            "leader": "某芯片",
            "leader_code": "sz300000",
        },
        {
            "code": "new_yh",
            "name": "银行",
            "change_pct": -1.2,
            "amount": 8e9,
            "leader": "某银行",
            "leader_code": "sh600000",
        },
    ]
    rank = p.industry_comparison(top_n=2, board="industry")
    assert rank["top"][0]["name"] == "半导体"
    assert "sina" in (rank.get("source") or "")
    flow = p.board_fund_flow(top_n=2)
    assert flow["inflow"]
    assert flow.get("metric") == "turnover"


def test_mock_index_valuation_and_breadth():
    p = MarketDataProvider(mode="mock")
    val = p.get_index_valuation("510300")
    assert val["index_code"] == "000300"
    assert val["pe"] > 0
    assert val["pe_ttm"] > 0
    assert val["pe_percentile"] is not None
    assert val["pb"] is not None
    assert val["pb_status"] == "ok"
    assert val["pb_percentile_status"] == "unavailable"
    assert val["pe_percentile_1y"] is not None
    assert val["pe_percentile_3y"] is not None
    assert val["pe_percentile_5y"] is not None
    assert val["pe_percentile_long_basis"] == "csi_pe_ttm_history"
    assert val["pe_percentile_window_start"]
    assert val["pe_percentile_window_end"]
    # 未在主指数快照覆盖内的代码：PB 仍 unavailable，但长窗口 PE 分位仍有
    bare = p.get_index_valuation("000852")
    assert bare["pb_status"] == "unavailable"
    assert bare["pe_percentile_1y"] is not None
    breadth = p.get_market_breadth()
    assert breadth["up"] > 0
    assert any(x["label"] == "涨停" for x in breadth["histogram"])
    hsgt = p.get_hsgt_flow()
    assert hsgt["northbound"]["deal_amt_wan"] is not None
    assert hsgt["northbound"]["net_buy_status"] == "unavailable"
    assert hsgt["usable_for_style"] is False
    assert "510050" in {m["code"] for m in p.list_universe(stock_type="etf")}
    assert "512890" in {m["code"] for m in p.list_universe(stock_type="etf")}
    premium = p.get_etf_premium(["510300", "510050"])
    assert premium["count"] == 2
    assert premium["items"][0]["premium_pct"] is not None
    assert premium["items"][0]["share_change_status"] == "t1_only"


def test_overview_filters_concept_noise_and_compacts():
    p = MarketDataProvider(mode="mock")

    def noisy_concept(*, top_n=10, board="concept", **_kwargs):
        return {
            "board": board,
            "top": [
                {"rank": 1, "code": "BK0481", "name": "半导体", "change_pct": 2.0, "member_count": 80, "low_sample": False},
                {"rank": 2, "code": "BK1645", "name": "昨日打二板以上表现", "change_pct": 3.0, "member_count": 50, "low_sample": False},
                {"rank": 3, "code": "BK1675", "name": "历史新高", "change_pct": 2.5, "member_count": 40, "low_sample": False},
            ],
            "bottom": [
                {"rank": 99, "code": "BK1050", "name": "昨日涨停_含一字", "change_pct": -1, "member_count": 30, "low_sample": False},
                {"rank": 98, "code": "BK1750", "name": "2026中报扭亏", "change_pct": -1.2, "member_count": 30, "low_sample": False},
            ],
            "total": 5,
            "filtered_noise": [
                {"code": "BK1645", "name": "昨日打二板以上表现", "side": "top"},
                {"code": "BK1675", "name": "历史新高", "side": "top"},
                {"code": "BK1050", "name": "昨日涨停_含一字", "side": "bottom"},
                {"code": "BK1750", "name": "2026中报扭亏", "side": "bottom"},
            ],
            "filtered_noise_count": 4,
        }

    p.industry_comparison = (  # type: ignore[method-assign]
        lambda **kwargs: noisy_concept(**kwargs)
        if kwargs.get("board") == "concept"
        else {
            "board": "industry",
            "top": [
                {"rank": 1, "code": "BK01", "name": "焦炭II", "change_pct": 2, "member_count": 40, "low_sample": False},
                {"rank": 2, "code": "BK02", "name": "焦炭III", "change_pct": 1.5, "member_count": 20, "low_sample": False},
                {"rank": 3, "code": "BK03", "name": "银行", "change_pct": 1, "member_count": 50, "low_sample": False},
            ],
            "bottom": [],
            "total": 3,
        }
    )
    overview = p.get_market_overview(top_n=3)
    hot_names = [x["name"] for x in overview["concept"]["hot"]]
    assert "半导体" in hot_names
    assert all("涨停" not in n and "新高" not in n and "打二板" not in n for n in hot_names)
    assert overview["concept"]["filtered_noise_count"] >= 1
    assert "hsgt" in overview
    leaders = [x["name"] for x in overview["industry"]["leaders"]]
    assert "焦炭II" in leaders
    assert "焦炭III" not in leaders
    assert overview["industry_fund_flow"]["top_n"] <= 4
    assert "main_net" in (overview["industry_fund_flow"]["inflow"][0] or {})


def test_dedupe_prefers_primary_over_ii_iii():
    from market_data.provider import _dedupe_board_rows

    rows = _dedupe_board_rows(
        [
            {"code": "BK1382", "name": "地面兵装Ⅲ", "change_pct": 1.2},
            {"code": "BK1229", "name": "地面兵装Ⅱ", "change_pct": 1.2},
            {"code": "BK01", "name": "银行", "change_pct": 0.5},
        ]
    )
    names = [r["name"] for r in rows]
    assert "地面兵装Ⅱ" in names
    assert "地面兵装Ⅲ" not in names
    assert "银行" in names


def test_mock_industry_bottom_is_weakest_first():
    p = MarketDataProvider(mode="mock")
    rank = p.industry_comparison(top_n=3, board="industry")
    assert rank["bottom_order"] == "weakest_first"
    assert rank["bottom"][0]["change_pct"] <= rank["bottom"][-1]["change_pct"]
    flow = p.board_fund_flow(board_type="industry", period="today", top_n=3)
    assert flow["outflow"]
    assert flow.get("outflow_basis") in ("main_net_negative", "least_inflow") or flow["outflow"][0][
        "main_net"
    ] < 0


def test_concept_noise_covers_report_labels():
    from market_data.provider import _is_concept_noise

    assert _is_concept_noise({"code": "BK1199", "name": "2025三季报预减"})
    assert _is_concept_noise({"code": "BK1750", "name": "2026中报扭亏"})
    assert not _is_concept_noise({"code": "BK0481", "name": "半导体"})


def test_dedupe_parent_child_boards():
    from market_data.provider import _dedupe_parent_child_boards

    rows = _dedupe_parent_child_boards(
        [
            {"code": "BK1201", "name": "电子", "main_net": 1},
            {"code": "BK1036", "name": "半导体", "main_net": 2},
            {"code": "BK0437", "name": "煤炭", "main_net": 3},
            {"code": "BK1493", "name": "动力煤", "main_net": 4},
            {"code": "BK1250", "name": "煤炭开采", "main_net": 5},
        ]
    )
    names = [r["name"] for r in rows]
    assert "电子" in names
    assert "半导体" not in names
    assert "煤炭" in names
    assert "动力煤" not in names
    assert "煤炭开采" not in names


def test_mock_etf_premium_marks_iopv_basis():
    p = MarketDataProvider(mode="mock")
    premium = p.get_etf_premium(["510300"])
    item = premium["items"][0]
    assert item["premium_basis"] == "iopv"
    assert item["nav_is_stale"] is False
    assert item["iopv_status"] == "ok"


def test_parse_cn_month_from_macro_title():
    from market_data.news import _parse_cn_month_from_macro_title

    assert _parse_cn_month_from_macro_title("9月CPI同比", "2026-10-14 09:30") == "2026-09"
    assert _parse_cn_month_from_macro_title("2026年09月PPI同比", "2026-10-14 09:30") == "2026-09"


def test_backfill_china_cpi_ppi_marks_released():
    from market_data import news as news_mod

    items = [
        {
            "time": "2026-10-14 09:30",
            "country": "中国",
            "title": "9月CPI同比",
            "kind": "data",
            "importance": 3,
            "actual": None,
            "actual_status": "unavailable",
            "forecast": None,
            "previous": 0.8,
            "unit": "%",
        },
        {
            "time": "2026-10-14 09:30",
            "country": "中国",
            "title": "9月PPI同比",
            "kind": "data",
            "importance": 3,
            "actual": None,
            "actual_status": "unavailable",
            "forecast": None,
            "previous": 3.8,
            "unit": "%",
        },
    ]

    def fake_series(report_name: str, *, page_size: int = 8):
        if report_name == "RPT_ECONOMY_CPI":
            return [{"REPORT_DATE": "2026-09-01 00:00:00", "NATIONAL_SAME": 0.9}]
        if report_name == "RPT_ECONOMY_PPI":
            return [{"REPORT_DATE": "2026-09-01 00:00:00", "BASE_SAME": 5.0}]
        return []

    news_mod._eastmoney_economy_series = fake_series  # type: ignore[assignment]
    out = news_mod._backfill_china_cpi_ppi(items)
    assert out[0]["actual"] == 0.9
    assert out[0]["actual_status"] == "released"
    assert out[0]["actual_source"]
    assert out[1]["actual"] == 5.0
    assert out[1]["actual_status"] == "released"


def test_mock_etf_premium_includes_t1_share_change():
    p = MarketDataProvider(mode="mock")
    premium = p.get_etf_premium(["510300", "510880"])
    assert premium["share_as_of"]
    assert premium["share_usable_for_intraday"] is False
    item = premium["items"][0]
    assert item["share_change_status"] == "t1_only"
    assert item["share"] is not None
    assert item["share_change"] is not None
    assert item["estimated_net_flow"] is not None
    assert item["share_as_of"] == premium["share_as_of"]


def test_mock_national_team_flow_is_honest_proxy():
    p = MarketDataProvider(mode="mock")
    out = p.get_national_team_flow(codes=["510300", "510050"])
    assert out["attribution"] == "proxy"
    assert out["usable_as_realtime_nt_orders"] is False
    assert out["count"] == 2
    item = out["items"][0]
    assert item["code"] == "510300"
    assert item["share"] is not None
    assert item["share_change"] is not None
    assert item["flow_status"] == "ok"
    assert item["estimated_net_flow"] is not None
    assert any(h.get("is_nt") for h in item["nt_holders"])
    assert out["summary"]["estimated_net_flow_sum"] is not None
    assert "holder_report_period" in item
    assert "staleness_days" in item
    assert item["holder_status"] in ("ok", "stale", "ok_no_nt", "unavailable", "skipped")


def test_nt_holder_keyword_match():
    assert MarketDataProvider._is_national_team_holder("中央汇金投资有限责任公司")
    assert MarketDataProvider._is_national_team_holder("中国证券金融股份有限公司")
    assert MarketDataProvider._is_national_team_holder("全国社保基金一一八组合")
    assert not MarketDataProvider._is_national_team_holder("某某私募基金管理有限公司")


def test_mock_index_valuation_maps_dividend_index_name():
    p = MarketDataProvider(mode="mock")
    val = p.get_index_valuation("000015")
    assert val["name"] == "红利指数"
    assert val["index_code"] == "000015"
    assert val["pb_status"] == "ok"
    assert val["pe_percentile_1y"] is not None
    assert "ulist_error" not in val or val.get("degraded") is True


def test_pe_ttm_window_percentiles_helper():
    from datetime import date, timedelta

    from market_data import asd_core

    end = date(2026, 10, 16)
    hist = []
    # 构造：近 1 年偏高、更早更低，使 latest 在 1y 低分位、全样本中位附近
    for i in range(400):
        d = end - timedelta(days=400 - i)
        pe = 10.0 + (i / 400.0) * 5.0  # 10 → 15
        hist.append({"date": d.isoformat(), "pe_ttm": pe})
    hist.append({"date": end.isoformat(), "pe_ttm": 12.0})
    pct = asd_core.pe_ttm_window_percentiles(hist, windows={"1y": 365, "3y": 365 * 3})
    assert pct["pe_ttm"] == 12.0
    assert pct["pe_percentile_long_basis"] == "csi_pe_ttm_history"
    assert pct["pe_percentile_1y"] is not None
    assert 0 <= pct["pe_percentile_1y"] <= 100
    assert pct["pe_percentile_1y_sample_size"] >= 20


def test_mock_macro_has_actual_status():
    p = MarketDataProvider(mode="mock")
    dig = p.get_macro_digest(days_ahead=5, days_back=1)
    assert dig["calendar"]
    assert all("actual_status" in x for x in dig["calendar"])


def test_hsgt_rejects_quota_as_net_buy():
    assert MarketDataProvider._net_buy_usable(0.0, 0.0, 0.0) is False
    assert MarketDataProvider._net_buy_usable(100.0, 40.0, 60.0) is True
    assert MarketDataProvider._net_buy_usable(100.0, 40.0, 10.0) is False


def test_num_or_none_cleans_eastmoney_dashes():
    assert MarketDataProvider._num_or_none("-") is None
    assert MarketDataProvider._num_or_none("--") is None
    assert MarketDataProvider._num_or_none("16.21") == 16.21
    assert MarketDataProvider._num_or_none(0) == 0.0


def test_mock_screen_etf_enriches_index_valuation():
    p = MarketDataProvider(mode="mock")
    etf = p.screen_market(stock_type="etf", top_n=30)
    mapped = [c for c in etf["candidates"] if c.get("index_code")]
    assert mapped, etf["candidates"][:3]
    hit = mapped[0]
    assert hit["pe_ttm"] is not None
    assert hit.get("pe_ttm") != 0
    assert hit["mapping_confidence"] == "exact"
    assert hit["valuation_status"] in ("ok", "partial")
    # 快照 0 不得当估值
    assert all(c.get("pe_ttm") != 0 for c in etf["candidates"])


def test_overview_min_member_skips_low_sample_main_board():
    p = MarketDataProvider(mode="mock")

    def tiny(**kwargs):
        return {
            "board": kwargs.get("board") or "industry",
            "top": [
                {
                    "rank": 1,
                    "code": "BK01",
                    "name": "会展服务",
                    "change_pct": 5,
                    "up_count": 1,
                    "down_count": 1,
                    "member_count": 2,
                    "low_sample": True,
                },
                {
                    "rank": 2,
                    "code": "BK02",
                    "name": "电子",
                    "change_pct": 3,
                    "up_count": 40,
                    "down_count": 20,
                    "member_count": 60,
                    "low_sample": False,
                },
            ],
            "bottom": [],
            "total": 2,
        }

    p.industry_comparison = tiny  # type: ignore[method-assign]
    overview = p.get_market_overview(top_n=3, min_member_count=5)
    leaders = [x["name"] for x in overview["industry"]["leaders"]]
    assert "电子" in leaders
    assert "会展服务" not in leaders
    assert any(x["name"] == "会展服务" for x in overview["industry"]["raw_leaders"])
    assert overview["industry"]["leaders"][0].get("member_count") == 60


def test_mock_breadth_has_aggregates_and_as_of(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_BREADTH_CACHE", str(tmp_path / "breadth"))
    # reload cache dir binding
    import market_data.provider as prov

    monkeypatch.setattr(prov, "_BREADTH_CACHE_DIR", tmp_path / "breadth")
    p = MarketDataProvider(mode="mock")
    b = p.get_market_breadth(days=1)
    assert b["as_of"]
    assert b["limit_up_total"] == 22
    assert b["limit_down_total"] == 8
    assert b["history_status"] == "ok"
    b2 = p.get_market_breadth(days=5)
    assert b2["history_status"] in ("ok", "partial")
    assert len(b2["history"]) >= 2
    overview = p.get_market_overview(top_n=3)
    assert overview["breadth"]["as_of"]
    assert overview["breadth"]["limit_down_total"] is not None


def test_mock_macro_prioritizes_importance_and_meta():
    from market_data.news import prioritize_macro_calendar

    rows = [
        {"time": "2026-10-20 10:00", "title": "低价值", "importance": 2, "kind": "data"},
        {"time": "2026-10-25 22:00", "title": "FOMC", "importance": 4, "kind": "event"},
        {"time": "2026-10-21 09:00", "title": "中价值", "importance": 2, "kind": "data"},
        {"time": "2026-10-24 20:00", "title": "核心PCE", "importance": 3, "kind": "data"},
    ]
    out = prioritize_macro_calendar(rows, limit=2, order_by="importance_desc_time_asc")
    assert out["truncated"] is True
    titles = [x["title"] for x in out["items"]]
    assert "FOMC" in titles
    assert "核心PCE" in titles
    assert "低价值" not in titles
    dig = MarketDataProvider(mode="mock").get_macro_digest(days_ahead=5, limit=10)
    assert dig["calendar_meta"]["returned_count"] == len(dig["calendar"])
    assert dig["calendar"][0]["importance"] >= dig["calendar"][-1]["importance"]


def test_mock_etf_premium_share_direction():
    p = MarketDataProvider(mode="mock")
    prem = p.get_etf_premium(["510300", "510330", "510050"])
    assert "share_direction" in prem
    assert prem["items"][0].get("index_code") in ("000300", "000016")
    assert "technical_adjustment_flag" in prem["items"][0]


def test_mock_index_valuation_dividend_caveat():
    p = MarketDataProvider(mode="mock")
    val = p.get_index_valuation("000015")
    assert val["caveat"]
    assert val["dy_percentile_1y"] is not None


def test_series_window_percentiles_helper():
    from datetime import date, timedelta

    from market_data.asd_core import series_window_percentiles

    today = date.today()
    hist = [
        {"date": (today - timedelta(days=i)).isoformat(), "dividend_yield": 3.0 + i * 0.01}
        for i in range(40)
    ]
    hist.reverse()
    out = series_window_percentiles(
        hist,
        value_key="dividend_yield",
        prefix="dy_percentile",
        min_samples=8,
        basis="test",
    )
    assert out["dividend_yield"] is not None
    assert out["dy_percentile_1y"] is not None
    assert out["series"]