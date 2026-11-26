"""The keyless calendars answer when FMP cannot and the free-fallback switch is on.

The conftest turns the switch off for every test; these cases turn it back on and
stub the stand-in readers, so the paid path's contract errors stay untouched.
"""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio

_MOD = "plugins.langalpha_market_data.macro_mcp_server"
_EARNINGS = f"{_MOD}.earnings_calendar_async"
_ECONOMIC = f"{_MOD}.economic_calendar_async"

_ROW = {"symbol": "AAPL", "date": "2025-01-29", "epsActual": 2.4, "epsEstimated": 2.35}


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "1")


async def _earnings():
    from importlib import import_module

    return await import_module(_MOD).get_earnings_calendar("2025-01-27", "2025-01-31")


async def test_no_key_takes_yahoo_and_names_the_source(switch_on):
    yahoo = AsyncMock(return_value=([_ROW], False))
    with (
        patch(f"{_MOD}.get_fmp_client", side_effect=ValueError("no key")),
        patch(_EARNINGS, yahoo),
    ):
        answer = await _earnings()

    assert answer["source"] == "yfinance"
    assert answer["data"] == [_ROW]
    assert answer["data_type"] == "earnings_calendar"
    assert yahoo.await_args.args == ("2025-01-27", "2025-01-31")


async def test_switch_off_keeps_the_contract_error(monkeypatch):
    monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "0")
    yahoo = AsyncMock(return_value=([_ROW], False))
    with (
        patch(f"{_MOD}.get_fmp_client", side_effect=ValueError("no key")),
        patch(_EARNINGS, yahoo),
    ):
        answer = await _earnings()

    assert answer["error"] == "client_unavailable"
    assert "data" not in answer
    yahoo.assert_not_awaited()


async def test_empty_paid_rows_take_yahoo(switch_on):
    client = AsyncMock()
    client.get_earnings_calendar_by_date.return_value = []
    with (
        patch(f"{_MOD}.get_fmp_client", return_value=client),
        patch(_EARNINGS, AsyncMock(return_value=([_ROW], False))),
    ):
        answer = await _earnings()

    assert answer["source"] == "yfinance"
    assert answer["data"] == [_ROW]


async def test_paid_rows_win_over_yahoo(switch_on):
    paid = {**_ROW, "symbol": "MSFT"}
    client = AsyncMock()
    client.get_earnings_calendar_by_date.return_value = [paid]
    yahoo = AsyncMock(return_value=([_ROW], False))
    with (
        patch(f"{_MOD}.get_fmp_client", return_value=client),
        patch(_EARNINGS, yahoo),
    ):
        answer = await _earnings()

    assert answer["source"] == "fmp"
    assert answer["data"] == [paid]
    yahoo.assert_not_awaited()


async def test_yahoo_failing_leaves_the_paid_error(switch_on):
    client = AsyncMock()
    client.get_earnings_calendar_by_date.side_effect = RuntimeError("boom SECRET")
    with (
        patch(f"{_MOD}.get_fmp_client", return_value=client),
        patch(_EARNINGS, AsyncMock(side_effect=RuntimeError("boom SECRET"))),
    ):
        answer = await _earnings()

    assert answer["error"] == "upstream_error"
    assert "SECRET" not in answer["detail"]
    assert "data" not in answer


async def test_economic_calendar_reads_the_week_files_without_a_key(switch_on):
    event = {"date": "2025-01-28 10:00:00", "country": "US", "event": "CPI", "currency": "USD"}
    with (
        patch(f"{_MOD}.get_fmp_client", side_effect=ValueError("no key")),
        patch(_ECONOMIC, AsyncMock(return_value=[event])),
    ):
        from importlib import import_module

        answer = await import_module(_MOD).get_economic_calendar("2025-01-27", "2025-01-31")

    assert answer["source"] == "forexfactory"
    assert answer["timezone"] == "UTC"
    assert answer["data"] == [event]


async def test_economic_calendar_switch_off_skips_forexfactory(monkeypatch):
    monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "0")
    spare = AsyncMock(return_value=[{"event": "CPI"}])
    with (
        patch(f"{_MOD}.get_fmp_client", side_effect=ValueError("no key")),
        patch(_ECONOMIC, spare),
    ):
        from importlib import import_module

        answer = await import_module(_MOD).get_economic_calendar("2025-01-27", "2025-01-31")

    assert answer["error"] == "client_unavailable"
    spare.assert_not_awaited()
