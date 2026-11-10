#!/usr/bin/env python3
"""Macro MCP Server.

Raw FMP macro data — economic indicators, treasury rates, risk premium, and
event calendars — via MCP. Payloads stay vendor-native inside `data`; the
envelope around them is the standard market-data contract (AGENT_CONTRACT.md).

Tools:
- get_economic_indicator: Time series for GDP, CPI, unemployment, etc.
- get_economic_calendar: Upcoming macro events with prior/estimate/actual values
- get_treasury_rates: Full yield curve (1M to 30Y)
- get_market_risk_premium: Risk premium by country for CAPM/WACC
- get_earnings_calendar: All companies reporting in a date range
"""

# NOTE: Tool docstrings in this file are hand-tuned agent prompt surface (parsed
# into agent prompts and generated sandbox wrappers) and are content-pinned by
# tests/unit/mcp_servers/test_agent_contract.py. Read mcp_servers/AGENT_CONTRACT.md
# before editing; intentional changes must regenerate agent_docstring_lock.json.

from __future__ import annotations

try:
    from _bootstrap import MCPServer  # script launch: mcp_servers/ is sys.path[0]
except ModuleNotFoundError:  # imported as a package module (tests)
    from mcp_servers._bootstrap import MCPServer

from typing import Optional

import asyncio
import json
import time
import urllib.request
from datetime import datetime, timezone


from data_client.fmp import get_fmp_client, fmp_lifespan
from mcp_servers._dates import calendar_days
from mcp_servers._envelope import error_from_exception, make_error, make_response
from mcp_servers._schemas import (
    NULLABLE_STR,
    RECORDS,
    STR,
    described,
    envelope_schema,
    output_model,
)


mcp = MCPServer("MacroMCP", lifespan=fmp_lifespan)

_SOURCE = "fmp"
_CLIENT_UNAVAILABLE = "FMP client is unavailable"
_UPSTREAM_FAILED = "FMP request failed"

# The tool documents these two names, but FMP serves the series under others and
# answers the documented ones with a non-JSON error. Every other name is FMP's own.
_FMP_INDICATOR_NAMES = {
    "federalFundsRate": "federalFunds",
    "nonFarmPayrolls": "totalNonfarmPayroll",
}


_OUT_GET_ECONOMIC_INDICATOR = output_model(
    "GetEconomicIndicatorOut",
    envelope_schema(
        RECORDS,
        echo={
            "data_type": STR,
            "indicator": described(STR, "Echoed indicator name."),
        },
    ),
)


@mcp.tool()
async def get_economic_indicator(
    name: str,
    limit: int = 50,
) -> _OUT_GET_ECONOMIC_INDICATOR:
    """Fetch an economic indicator time series — GDP growth for the macro
    outlook, CPI/inflation for discount-rate assumptions, or unemployment and
    the Fed funds rate for economic context.

    Args:
        name: Indicator — "GDP", "CPI", "unemploymentRate", "federalFundsRate",
            "inflationRate", "retailSales", "consumerSentiment", "nonFarmPayrolls".
        limit: Number of observations to fetch (default 50).

    Returns:
        dict: {count, data, source, data_type, indicator}. data is a list of
        observations; count is the observation total. Fields: date, value (and
        for some series, name). Field names are FMP-native camelCase; date is
        "YYYY-MM-DD"; observations are newest-first as returned by FMP. On error:
        {error: <code>, detail, indicator}.
    """
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE, indicator=name)

    try:
        data = await client.get_economic_indicators(
            _FMP_INDICATOR_NAMES.get(name, name), limit=limit
        )

        return make_response(
            data or [],
            source=_SOURCE,
            data_type="economic_indicator",
            indicator=name,
        )

    except Exception as e:  # noqa: BLE001
        return error_from_exception(e, _UPSTREAM_FAILED, indicator=name)


_OUT_GET_ECONOMIC_CALENDAR = output_model(
    "GetEconomicCalendarOut",
    envelope_schema(
        RECORDS,
        echo={
            "data_type": STR,
            "from_date": NULLABLE_STR,
            "to_date": NULLABLE_STR,
        },
    ),
)


@mcp.tool()
async def get_economic_calendar(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> _OUT_GET_ECONOMIC_CALENDAR:
    """Fetch economic events with prior, estimate, and actual values — build a
    catalyst calendar, generate a morning note, or track Fed meetings, jobs
    reports, and CPI releases.

    Args:
        from_date: Start date "YYYY-MM-DD" (default: today).
        to_date: End date "YYYY-MM-DD" (default: 7 days from today).

    Returns:
        dict: {count, data, source, data_type, from_date, to_date}. data is a
        list of events; count is the event total. Fields: date, country, event,
        currency, previous, estimate, actual, change, changePercentage, impact,
        unit. actual/estimate/previous are null for events not yet released.
        Field names are FMP-native camelCase; date is "YYYY-MM-DD HH:MM:SS";
        order is as returned by FMP. On error: {error: <code>, detail}.
    """
    try:
        lo, hi = calendar_days(from_date, to_date)
    except Exception as e:  # noqa: BLE001
        return error_from_exception(e, _UPSTREAM_FAILED)

    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        events = await _fallback_calendar(lo, hi)
        if events:
            return _fallback_response(events, from_date, to_date)
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE)

    try:
        data = await client.get_economic_calendar(from_date=lo, to_date=hi)
        if not data:
            events = await _fallback_calendar(lo, hi)
            if events:
                return _fallback_response(events, from_date, to_date)

        return make_response(
            data or [],
            source=_SOURCE,
            data_type="economic_calendar",
            from_date=from_date,
            to_date=to_date,
        )

    except Exception as e:  # noqa: BLE001
        events = await _fallback_calendar(lo, hi)
        if events:
            return _fallback_response(events, from_date, to_date)
        return error_from_exception(e, _UPSTREAM_FAILED)


_OUT_GET_TREASURY_RATES = output_model(
    "GetTreasuryRatesOut",
    envelope_schema(
        RECORDS,
        echo={
            "data_type": STR,
            "from_date": NULLABLE_STR,
            "to_date": NULLABLE_STR,
        },
    ),
)


@mcp.tool()
async def get_treasury_rates(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> _OUT_GET_TREASURY_RATES:
    """Fetch US Treasury rates across the full yield curve (1M to 30Y) — get the
    risk-free rate for DCF/WACC (typically 10Y), read the curve shape, or track
    rate trends.

    Args:
        from_date: Start date "YYYY-MM-DD" (default: recent data).
        to_date: End date "YYYY-MM-DD" (default: today).

    Returns:
        dict: {count, data, source, data_type, from_date, to_date}. data is a
        list of daily records; count is the record total. Each record has a date
        plus per-tenor rate columns: month1, month2, month3, month6, year1,
        year2, year3, year5, year7, year10, year20, year30. Rates are percent
        (4.5 = 4.5%). Field names are FMP-native camelCase; date is "YYYY-MM-DD";
        records are newest-first as returned by FMP. On error:
        {error: <code>, detail}.
    """
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE)

    try:
        lo, hi = calendar_days(from_date, to_date)
        data = await client.get_treasury_rates(from_date=lo, to_date=hi)

        return make_response(
            data or [],
            source=_SOURCE,
            data_type="treasury_rates",
            from_date=from_date,
            to_date=to_date,
        )

    except Exception as e:  # noqa: BLE001
        return error_from_exception(e, _UPSTREAM_FAILED)


_OUT_GET_MARKET_RISK_PREMIUM = output_model(
    "GetMarketRiskPremiumOut",
    envelope_schema(RECORDS, echo={"data_type": STR}),
)


@mcp.tool()
async def get_market_risk_premium() -> _OUT_GET_MARKET_RISK_PREMIUM:
    """Fetch market risk premium by country for CAPM/WACC — get the equity risk
    premium for a DCF cost of equity, or compare premiums across markets.

    Returns:
        dict: {count, data, source, data_type}. data is a list of country
        records; count is the record total. Fields: country, continent,
        countryRiskPremium, totalEquityRiskPremium. Premiums are percent
        (5.5 = 5.5%). Field names are FMP-native camelCase; order is by country
        as returned by FMP (not time-ordered). On error: {error: <code>, detail}.
    """
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE)

    try:
        data = await client.get_market_risk_premium()

        return make_response(
            data or [],
            source=_SOURCE,
            data_type="market_risk_premium",
        )

    except Exception as e:  # noqa: BLE001
        return error_from_exception(e, _UPSTREAM_FAILED)


_OUT_GET_EARNINGS_CALENDAR = output_model(
    "GetEarningsCalendarOut",
    envelope_schema(
        RECORDS,
        echo={"data_type": STR, "from_date": STR, "to_date": STR},
    ),
)


@mcp.tool()
async def get_earnings_calendar(
    from_date: str,
    to_date: str,
) -> _OUT_GET_EARNINGS_CALENDAR:
    """Fetch the earnings calendar for all companies reporting in a date range —
    build a catalyst calendar, generate a morning note, or track earnings-season
    volume.

    Args:
        from_date: Start date "YYYY-MM-DD".
        to_date: End date "YYYY-MM-DD".

    Returns:
        dict: {count, data, source, data_type, from_date, to_date}. data is a
        list of reporter records; count is the record total. Fields: symbol,
        date, epsActual, epsEstimated, revenueActual, revenueEstimated,
        lastUpdated. actual fields are null before a company reports. Field names
        are FMP-native camelCase; date is "YYYY-MM-DD"; order is as returned by
        FMP. On error: {error: <code>, detail}.
    """
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE)

    try:
        lo, hi = calendar_days(from_date, to_date)
        data = await client.get_earnings_calendar_by_date(from_date=lo, to_date=hi)

        return make_response(
            data or [],
            source=_SOURCE,
            data_type="earnings_calendar",
            from_date=from_date,
            to_date=to_date,
        )

    except Exception as e:  # noqa: BLE001
        return error_from_exception(e, _UPSTREAM_FAILED)


if __name__ == "__main__":
    mcp.run(transport="stdio")


# --- Keyless calendar fallback -------------------------------------------
#
# FMP gates its calendar behind the Starter plan; the public forex-factory week
# files carry the same events with a UTC-able timestamp and an impact rating.
# Only used when FMP answers nothing, so the FMP path stays authoritative.

_FF_WEEKS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
)
_FF_TTL_SECONDS = 600.0
_FF_TIMEOUT_SECONDS = 20.0
_FF_CACHE: dict[str, tuple[float, list[dict[str, object]]]] = {}
_FF_COUNTRIES = {
    "USD": "US",
    "EUR": "EU",
    "GBP": "UK",
    "JPY": "JP",
    "CNY": "CN",
    "AUD": "AU",
    "NZD": "NZ",
    "CAD": "CA",
    "CHF": "CH",
}


def _ff_fetch(url: str) -> list[dict[str, object]]:
    """Read one week file, or nothing when the feed is unreachable."""
    now = time.monotonic()
    cached = _FF_CACHE.get(url)
    if cached is not None and now - cached[0] < _FF_TTL_SECONDS:
        return cached[1]
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=_FF_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    rows = payload if isinstance(payload, list) else []
    _FF_CACHE[url] = (now, rows)
    return rows


def _ff_stamp(raw: object) -> str:
    """Render an offset timestamp as naive UTC, the shape FMP returns."""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _fallback_calendar(lo: str, hi: str) -> list[dict[str, object]]:
    """Events between two YYYY-MM-DD dates, shaped like FMP's calendar rows."""
    rows: list[dict[str, object]] = []
    for url in _FF_WEEKS:
        try:
            rows.extend(await asyncio.to_thread(_ff_fetch, url))
        except Exception:  # noqa: BLE001
            continue

    events: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = _ff_stamp(row.get("date"))
        if not stamp:
            continue
        day = stamp[:10]
        if day < lo or day > hi:
            continue
        currency = str(row.get("country") or "").upper()
        events.append(
            {
                "date": stamp,
                "country": _FF_COUNTRIES.get(currency, currency),
                "event": str(row.get("title") or ""),
                "currency": currency,
                "previous": row.get("previous"),
                "estimate": row.get("forecast"),
                "actual": row.get("actual"),
                "change": None,
                "changePercentage": None,
                "impact": str(row.get("impact") or ""),
                "unit": None,
            }
        )

    events.sort(key=lambda event: (str(event["date"]), str(event["event"])))
    return events


def _fallback_response(events, from_date, to_date):
    """Wrap fallback rows in the standard market-data envelope."""
    return make_response(
        events,
        source="forexfactory",
        data_type="economic_calendar",
        from_date=from_date,
        to_date=to_date,
    )
