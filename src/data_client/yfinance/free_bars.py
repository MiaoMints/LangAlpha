"""Bars from free feeds, for the market-data servers to fall back on.

Yahoo Finance serves equities, indexes, rates and futures; Binance (through the
desk source) serves coins. Every function returns the internal bar shape
(``time`` in Unix ms, ascending), so a server passes the rows through the same
normalisation its paid feed uses and the caller sees one date convention.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..base import IntervalNotSupported
from ..normalize import minor_unit_places, symbol_timezone

from . import data_source as _ds
from .yahoo import yahoo_symbol

#: Request interval -> Yahoo history interval. ``4hour`` has no Yahoo step
#: and is built from hourly bars.
_STEPS: dict[str, str | None] = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "1hour": "1h",
    "4hour": None,
    "1day": "1d",
    "1week": "1wk",
    "1month": "1mo",
}

#: Index families whose Yahoo symbol differs from ``^<family>``.
_FAMILIES = {"SPX": "^GSPC", "COMP": "^IXIC"}

#: Metal spots trade as rates on Yahoo, the rest of a commodity as a front
#: future (``GC=F``). ``symbol`` arrives as the protocol's ``GCUSD``.
_SPOT = {
    "XAU": "XAUUSD=X",
    "XAUUSD": "XAUUSD=X",
    "XAG": "XAGUSD=X",
    "XAGUSD": "XAGUSD=X",
}
_COMMODITY = re.compile(r"^([A-Z]{1,3})(?:USD)?$")


def yahoo_free_symbol(symbol: str, asset_type: str, *, is_index: bool = False) -> str:
    """Yahoo's spelling of *symbol* for *asset_type* (stock, commodity, crypto, forex)."""
    s = symbol.strip().upper()
    if asset_type == "forex":
        # EUR-USD and EURUSD are both the rate EURUSD=X; left alone the first
        # reads as a since-delisted coin.
        return s if "=" in s else f"{s.replace('-', '')}=X"
    if "=" in s or "^" in s:
        return s  # already a vendor spelling
    if "-" in s:
        return s  # BTC-USD is Yahoo's own pair spelling
    if asset_type == "commodity":
        if s in _SPOT:
            return _SPOT[s]
        match = _COMMODITY.match(s)
        return f"{match.group(1)}=F" if match else s
    if asset_type in ("crypto", "forex"):
        return yahoo_symbol(s)
    return _FAMILIES.get(s) or yahoo_symbol(s, is_index=is_index)


def resample_hours(bars: list[dict[str, Any]], hours: int, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Fold hourly bars into *hours*-long buckets whose wall start is midnight in *tz*."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for bar in sorted(bars, key=lambda b: b["time"]):
        local = datetime.fromtimestamp(bar["time"] / 1000, tz)
        start = local.replace(hour=local.hour - local.hour % hours, minute=0, second=0, microsecond=0)
        key = int(start.timestamp() * 1000)
        if current is None or current["time"] != key:
            current = dict(bar, time=key)
            out.append(current)
            continue
        current["high"] = max(current["high"], bar["high"])
        current["low"] = min(current["low"], bar["low"])
        current["close"] = bar["close"]
        current["volume"] += bar["volume"]
    return out


async def free_bars(
    symbol: str,
    interval: str,
    from_date: str | None = None,
    to_date: str | None = None,
    *,
    asset_type: str = "stock",
    is_index: bool = False,
    yahoo: str | None = None,
) -> list[dict[str, Any]]:
    """Hourly-or-longer bars for *symbol* from Yahoo Finance; ``[]`` when it has none.

    ``asset_type`` is one of stock, commodity, crypto, forex (index bars pass
    ``is_index``). An interval Yahoo cannot serve raises ``IntervalNotSupported``.
    """
    if interval not in _STEPS:
        raise IntervalNotSupported(f"Interval '{interval}' is not supported by yfinance")
    api = yahoo or yahoo_free_symbol(symbol, asset_type, is_index=is_index)
    places = minor_unit_places(symbol)
    if interval in ("1day", "1week", "1month"):
        step = _STEPS[interval]
        assert step is not None  # guarded by the membership test above
        bars = await asyncio.to_thread(
            _ds._fetch_history, api, step, from_date, to_date, places, symbol_timezone(symbol)
        )
    else:
        hourly = interval == "4hour"
        bars = await asyncio.to_thread(
            _ds._fetch_history, api, "1h" if hourly else _STEPS[interval] or "", from_date, to_date, places
        )
        bars = _ds._place_in_sessions(bars, symbol, is_index, "1hour" if hourly else interval)
        if hourly:
            bars = resample_hours(bars, 4, symbol_timezone(symbol))
    return sorted(bars, key=lambda b: b["time"])


async def desk_bars(
    symbol: str,
    interval: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Coin bars straight from Binance (``BTC-USD``); ``[]`` when Binance has none."""
    from ..desk.market import DeskMarketSource

    if interval not in _STEPS:
        raise IntervalNotSupported(f"Interval '{interval}' is not supported by Binance")
    source = DeskMarketSource()
    try:
        if interval == "1day":
            return await source.get_daily(symbol, from_date, to_date)
        if interval in ("1week", "1month"):
            return []  # Binance klines stop at the daily step
        return await source.get_intraday(symbol, interval, from_date, to_date)
    finally:
        await source.close()
