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
    assert val["pb"] is None  # 中证估值文件不含 PB
    assert val["pb_status"] == "unavailable"
    assert val["pe_percentile_1y"] is None
    breadth = p.get_market_breadth()
    assert breadth["up"] > 0
    assert any(x["label"] == "涨停" for x in breadth["histogram"])
    hsgt = p.get_hsgt_flow()
    assert hsgt["northbound"]["day_net_wan"] != 0
    assert "510050" in {m["code"] for m in p.list_universe(stock_type="etf")}
    assert "512890" in {m["code"] for m in p.list_universe(stock_type="etf")}


def test_overview_filters_concept_noise_and_compacts():
    p = MarketDataProvider(mode="mock")

    def noisy_concept(*, top_n=10, board="concept"):
        return {
            "board": board,
            "top": [
                {"rank": 1, "code": "BK0481", "name": "半导体", "change_pct": 2.0},
                {"rank": 2, "code": "BK1645", "name": "昨日打二板以上表现", "change_pct": 3.0},
                {"rank": 3, "code": "BK1675", "name": "历史新高", "change_pct": 2.5},
            ],
            "bottom": [{"rank": 99, "code": "BK1050", "name": "昨日涨停_含一字", "change_pct": -1}],
            "total": 3,
        }

    p.industry_comparison = (  # type: ignore[method-assign]
        lambda **kwargs: noisy_concept(**kwargs)
        if kwargs.get("board") == "concept"
        else {
            "board": "industry",
            "top": [{"rank": 1, "code": "BK01", "name": "银行", "change_pct": 1}],
            "bottom": [],
            "total": 1,
        }
    )
    overview = p.get_market_overview(top_n=3)
    hot_names = [x["name"] for x in overview["concept"]["hot"]]
    assert "半导体" in hot_names
    assert all("涨停" not in n and "新高" not in n and "打二板" not in n for n in hot_names)
    assert overview["concept"]["filtered_noise_count"] >= 1
    assert "hsgt" in overview
    assert "main_net" in (overview["industry_fund_flow"]["inflow"][0] or {})


def test_num_or_none_cleans_eastmoney_dashes():
    assert MarketDataProvider._num_or_none("-") is None
    assert MarketDataProvider._num_or_none("--") is None
    assert MarketDataProvider._num_or_none("16.21") == 16.21
    assert MarketDataProvider._num_or_none(0) == 0.0