"""The calendar endpoints fall back to the keyless feeds when FMP cannot answer.

The router's own client and cache are stubbed, as in test_bars_api; the two
stand-in readers are patched where the router imports them.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio

_MOD = "src.server.app.calendar"
_EARNINGS = f"{_MOD}.earnings_calendar_async"
_ECONOMIC = f"{_MOD}.economic_calendar_async"

_EARNINGS_ROW = {"symbol": "AAPL", "date": "2025-01-29", "epsActual": 2.4, "epsEstimated": 2.35}
_ECONOMIC_ROW = {
    "date": "2025-01-28 10:00:00",
    "country": "US",
    "event": "CPI",
    "currency": "USD",
    "impact": "High",
}


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "1")


@pytest_asyncio.fixture
async def client():
    from src.server.app.calendar import router

    app = create_test_app(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class _NoCache:
    async def get(self, *_args, **_kwargs):
        return None

    async def set(self, *_args, **_kwargs):
        return None


async def test_earnings_without_a_key_read_yahoo(client, switch_on):
    yahoo = AsyncMock(return_value=([_EARNINGS_ROW], False))
    with (
        patch(f"{_MOD}.FMPClient", side_effect=ValueError("no key")),
        patch(f"{_MOD}._earnings_cache", _NoCache()),
        patch(_EARNINGS, yahoo),
    ):
        resp = await client.get("/api/v1/calendar/earnings?from=2025-01-27&to=2025-01-31")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "yfinance"
    assert body["count"] == 1
    assert body["data"][0]["symbol"] == "AAPL"
    assert yahoo.await_args.args == ("2025-01-27", "2025-01-31")


async def test_earnings_switch_off_stays_empty(client, monkeypatch):
    monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "0")
    yahoo = AsyncMock(return_value=([_EARNINGS_ROW], False))
    with (
        patch(f"{_MOD}.FMPClient", side_effect=ValueError("no key")),
        patch(f"{_MOD}._earnings_cache", _NoCache()),
        patch(_EARNINGS, yahoo),
    ):
        resp = await client.get("/api/v1/calendar/earnings?from=2025-01-27&to=2025-01-31")

    assert resp.status_code == 200
    assert resp.json() == {"data": [], "count": 0, "source": "fmp"}
    yahoo.assert_not_awaited()


async def test_economic_fmp_error_reads_the_week_files(client, switch_on):
    paid = MagicMock()
    paid.get_economic_calendar = AsyncMock(side_effect=RuntimeError("402 Payment Required"))
    paid.close = AsyncMock()
    with (
        patch(f"{_MOD}.FMPClient", return_value=paid),
        patch(_ECONOMIC, AsyncMock(return_value=[_ECONOMIC_ROW])),
    ):
        resp = await client.get("/api/v1/calendar/economic?from=2025-01-27&to=2025-01-31")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "forexfactory"
    assert body["data"][0]["event"] == "CPI"
    paid.close.assert_awaited_once()


async def test_economic_paid_rows_stay_fmp(client, switch_on):
    paid = MagicMock()
    paid.get_economic_calendar = AsyncMock(return_value=[_ECONOMIC_ROW])
    paid.close = AsyncMock()
    spare = AsyncMock(return_value=[_ECONOMIC_ROW])
    with (
        patch(f"{_MOD}.FMPClient", return_value=paid),
        patch(_ECONOMIC, spare),
    ):
        resp = await client.get("/api/v1/calendar/economic?from=2025-01-27&to=2025-01-31")

    assert resp.json()["source"] == "fmp"
    spare.assert_not_awaited()


async def test_economic_without_a_key_switch_off_stays_empty(client, monkeypatch):
    monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "0")
    spare = AsyncMock(return_value=[_ECONOMIC_ROW])
    with (
        patch(f"{_MOD}.FMPClient", side_effect=ValueError("no key")),
        patch(_ECONOMIC, spare),
    ):
        resp = await client.get("/api/v1/calendar/economic?from=2025-01-27&to=2025-01-31")

    assert resp.status_code == 200
    assert resp.json() == {"data": [], "count": 0, "source": "fmp"}
    spare.assert_not_awaited()
