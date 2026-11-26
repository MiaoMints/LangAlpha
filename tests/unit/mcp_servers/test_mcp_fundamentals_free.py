"""Free stand-ins for the FMP-only fundamentals tools (switch on, FMP down)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

_MOD = "plugins.langalpha_market_data.fundamentals_mcp_server"
_READ = "data_client.yfinance.free_fundamentals.read"
_BARS = "data_client.yfinance.free_bars.free_bars"

_MARKED = ("financial_statements", "financial_ratios", "growth_metrics", "dividends_and_splits", "shares_float", "key_executives")
_PLAIN = ("historical_valuation", "analyst_ratings", "analyst_estimates", "earnings_surprises", "insider_trades")

_TOOL = {
    "financial_statements": "get_financial_statements",
    "financial_ratios": "get_financial_ratios",
    "growth_metrics": "get_growth_metrics",
    "dividends_and_splits": "get_dividends_and_splits",
    "shares_float": "get_shares_float",
    "key_executives": "get_key_executives",
    "historical_valuation": "get_historical_valuation",
    "analyst_ratings": "get_analyst_ratings",
    "analyst_estimates": "get_analyst_estimates",
    "earnings_surprises": "get_earnings_surprises",
    "insider_trades": "get_insider_trades",
    "technical_indicator": "get_technical_indicator",
}
_ROW = {"row": 1}


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "1")


def _tool(data_type: str):
    import importlib

    return getattr(importlib.import_module(_MOD), _TOOL[data_type])


@pytest.mark.parametrize("data_type", _MARKED)
@pytest.mark.asyncio
async def test_marked_tools_name_fmp_as_the_failed_source(switch_on, data_type):
    calls = []

    def read(kind, symbol, asked):
        calls.append((kind, symbol, asked))
        return [{"row": kind}]

    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_READ, side_effect=read):
        result = await _tool(data_type)("AAPL")

    assert_ok = "error" not in result and result["data"] == [{"row": data_type}]
    assert assert_ok, result
    assert result["source"] == "yfinance"
    assert result["fallback_from"] == "fmp"
    assert result["data_type"] == data_type
    assert calls[0][0] == data_type and calls[0][1] == "AAPL"


@pytest.mark.parametrize("data_type", _PLAIN)
@pytest.mark.asyncio
async def test_plain_tools_take_yahoo_without_the_marker(switch_on, data_type):
    def read(kind, symbol, asked):
        return [{"row": kind}]

    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_READ, side_effect=read):
        result = await _tool(data_type)("AAPL")

    assert "error" not in result and result["data"] == [{"row": data_type}], result
    assert result["source"] == "yfinance"
    assert "fallback_from" not in result


@pytest.mark.asyncio
async def test_switch_off_keeps_the_contract_error(monkeypatch):
    monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "0")
    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_READ) as read:
        result = await _tool("shares_float")("AAPL")

    assert result["error"] == "client_unavailable"
    assert "data" not in result
    read.assert_not_called()


@pytest.mark.asyncio
async def test_yahoo_empty_leaves_the_error(switch_on):
    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_READ, return_value={"dividends": [], "splits": []}):
        result = await _tool("dividends_and_splits")("AAPL")

    assert result["error"] == "client_unavailable"
    assert "data" not in result


@pytest.mark.asyncio
async def test_yahoo_crash_leaves_the_error(switch_on):
    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_READ, side_effect=ValueError("boom")):
        result = await _tool("key_executives")("AAPL")

    assert result["error"] == "client_unavailable"
    assert "boom" not in result["detail"]


@pytest.mark.asyncio
async def test_ratings_hand_yahoo_the_price_scale(switch_on):
    seen = {}

    def read(kind, symbol, asked):
        seen.update(asked)
        return {"target_consensus": [], "rating_counts": [{"date": "2025-01", "analystRatingsStrongBuy": 3}], "rating_actions": [], "price_targets": []}

    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_READ, side_effect=read):
        result = await _tool("analyst_ratings")("VOD.L")

    assert result["source"] == "yfinance"
    assert seen["places"] == 2
    assert seen["tz"] == "Europe/London"


def _bars(days: int = 60) -> list[dict]:
    start = datetime(2025, 1, 1, 14, 30, tzinfo=timezone.utc)
    rows = []
    for i in range(days):
        close = float(i + 1)
        rows.append({"time": int((start + timedelta(days=i)).timestamp() * 1000), "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1_000})
    return rows


@pytest.mark.parametrize(
    ("indicator", "expected"),
    [
        ("sma", 53.5),  # mean(close 47..60)
        ("wma", sum(w * v for w, v in zip(range(1, 15), range(47, 61))) / 105),
        ("ema", None),
        ("rsi", 100.0),
        ("williams", -100 / 15),  # -100 * (61 - 60) / (61 - 46)
        ("adx", 100.0),  # every bar steps up: +DM=1, -DM=0
    ],
)
@pytest.mark.asyncio
async def test_technical_indicator_computed_from_free_bars(switch_on, indicator, expected):
    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_BARS, return_value=_bars()):
        result = await _tool("technical_indicator")("AAPL", indicator, period=14, timeframe="1day")

    assert "error" not in result, result
    assert result["source"] == "yfinance"
    rows = result["data"]
    expected_len = {"adx": 33, "rsi": 46}.get(indicator, 47)
    expected_oldest = {"adx": "2025-01-28", "rsi": "2025-01-15"}.get(indicator, "2025-01-14")
    assert len(rows) == expected_len and result["count"] == len(rows)
    assert rows[0]["date"] == "2025-03-01" and rows[-1]["date"] == expected_oldest, rows[-1]
    newest = rows[0][indicator]
    if indicator == "ema":
        k = 2 / 15
        value = sum(range(1, 15)) / 14
        for close in range(15, 61):
            value = close * k + value * (1 - k)
        assert newest == pytest.approx(value, abs=1e-3)
    else:
        assert newest == pytest.approx(expected, abs=1e-4), rows[:3]
    assert {k for row in rows for k in row} >= {"date", "open", "high", "low", "close", "volume", indicator}
    assert result["indicator"] == indicator and result["period"] == 14 and result["timeframe"] == "1day"
    assert result["price_treatment"]


@pytest.mark.asyncio
async def test_technical_indicator_rsi_mixed_changes_and_500_cap_keeps_newest(switch_on):
    start = datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)
    # Alternating +2 / -1 moves produce gains > 0 and losses > 0 -> RSI must stay strictly in (0, 100).
    bars = []
    price = 100.0
    for i in range(550):
        price += 2.0 if i % 2 == 0 else -1.0
        bars.append({"time": int((start + timedelta(days=i)).timestamp() * 1000), "open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1000})

    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_BARS, return_value=bars):
        result = await _tool("technical_indicator")("AAPL", "rsi", period=14, timeframe="1day")

    assert "error" not in result, result
    rows = result["data"]
    assert len(rows) == 500
    expected_newest_date = (start + timedelta(days=549)).strftime("%Y-%m-%d")
    assert rows[0]["date"] == expected_newest_date
    # Wilder RSI for alternating +2 / -1 with period=14 converges near 2/(2+1)*100 = 66.67.
    assert 60.0 < rows[0]["rsi"] < 75.0, rows[0]


@pytest.mark.asyncio
async def test_technical_indicator_wins_when_fmp_answers(switch_on):
    client_rows = [{"date": "2025-01-01", "rsi": 55}]
    from unittest.mock import AsyncMock

    client = AsyncMock()
    client.get_technical_indicator = AsyncMock(return_value=client_rows)
    with patch(f"{_MOD}.get_fmp_client", return_value=client), patch(_BARS) as bars:
        result = await _tool("technical_indicator")("AAPL", "rsi")

    assert result["source"] == "fmp" and result["data"] == client_rows
    bars.assert_not_called()


@pytest.mark.asyncio
async def test_technical_indicator_keeps_the_error_when_yahoo_has_too_few_bars(switch_on):
    with patch(f"{_MOD}.get_fmp_client", side_effect=RuntimeError("402 Payment Required")), patch(_BARS, return_value=_bars(5)):
        result = await _tool("technical_indicator")("AAPL", "rsi")

    assert result["error"] == "client_unavailable" and "data" not in result
