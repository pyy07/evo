from market_data.provider import MarketDataProvider


def test_mock_snapshot_and_history():
    p = MarketDataProvider(mode="mock")
    quotes = p.get_market_snapshot(["510300", "511010"])
    assert "510300" in quotes
    assert quotes["510300"]["price"] > 0
    bars = p.get_etf_history("510300", count=10)
    assert len(bars) > 0
    cal = p.get_trading_calendar(2026, 3)
    assert len(cal) >= 28