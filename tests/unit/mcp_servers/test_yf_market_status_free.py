"""get_market_status keeps answering when Yahoo's status payload is thin or odd."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from plugins.yfinance.yf_market_mcp_server import get_market_status

from .conftest import assert_error, assert_ok_envelope


def _market(monkey, **attrs):
    market = MagicMock()
    for name, value in attrs.items():
        setattr(market, name, value)
    monkey.return_value = market
    return market


class TestThinAnswers:
    @patch("plugins.yfinance.yf_market_mcp_server.yf")
    def test_no_status_still_reports_the_summary(self, mock_yf):
        _market(mock_yf.Market, status=None, summary={"^GSPC": {"price": 6000}})

        result = get_market_status("US")

        assert_ok_envelope(result, source="yfinance", count=1)
        assert result["timezone"] == "UTC"
        assert result["data"]["summary"] == {"^GSPC": {"price": 6000}}

    @patch("plugins.yfinance.yf_market_mcp_server.yf")
    def test_a_summary_that_will_not_load_keeps_the_status(self, mock_yf):
        market = _market(mock_yf.Market, status="OPEN")

        def boom():
            raise RuntimeError("summary endpoint reset")

        type(market).summary = property(boom)

        result = get_market_status("US")

        assert_ok_envelope(result, source="yfinance", count=1)
        assert result["data"] == {"status": "OPEN", "summary": {}}

    @patch("plugins.yfinance.yf_market_mcp_server.yf")
    def test_unknown_zone_name_falls_back_to_utc(self, mock_yf):
        moment = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)
        _market(
            mock_yf.Market,
            status={"open": moment, "close": moment, "timezone": {"$text": "Mars/Olympus"}},
            summary={},
        )

        result = get_market_status("US")

        assert result["timezone"] == "UTC"

    @patch("plugins.yfinance.yf_market_mcp_server.yf")
    def test_plain_string_timezone_does_not_crash(self, mock_yf):
        _market(mock_yf.Market, status={"timezone": "America/New_York"}, summary={})

        result = get_market_status("US")

        assert_ok_envelope(result, source="yfinance", count=1)
        assert result["timezone"] == "UTC"


class TestRefusedMarket:
    @patch("plugins.yfinance.yf_market_mcp_server.yf")
    def test_yahoo_value_error_is_an_upstream_error(self, mock_yf):
        mock_yf.Market.side_effect = ValueError("Invalid market name")

        result = get_market_status("ATLANTIS")

        assert_error(result, "upstream_error")
        assert result["market"] == "ATLANTIS"

    @patch("plugins.yfinance.yf_market_mcp_server.yf")
    def test_refused_status_is_an_upstream_error(self, mock_yf):
        mock_yf.Market.side_effect = None
        market = MagicMock()

        def boom():
            raise ValueError("no such market")

        type(market).status = property(boom)
        mock_yf.Market.return_value = market

        result = get_market_status("ATLANTIS")

        assert_error(result, "upstream_error")
