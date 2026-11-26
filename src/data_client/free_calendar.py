"""Keyless stand-ins for the two calendar feeds FMP gates behind a paid plan.

The economic calendar comes from forex-factory's public week files, the earnings
calendar from Yahoo's calendar endpoint (US listings only, 100 rows a page).
Rows keep FMP's field names so callers shape them the same way.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import urllib.request
from datetime import datetime, timezone

FF_WEEKS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
)
FF_TTL_SECONDS = 600.0
FF_TIMEOUT_SECONDS = 20.0
FF_COUNTRIES = {
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

_EARNINGS_PAGE = 100  # Yahoo's cap per request
_EARNINGS_MAX_PAGES = 20

_ff_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}
_ff_lock = threading.Lock()


def ff_fetch(url: str) -> list[dict[str, object]]:
    """Read one week file, or nothing when the feed is unreachable."""
    now = time.monotonic()
    with _ff_lock:
        cached = _ff_cache.get(url)
    if cached is not None and now - cached[0] < FF_TTL_SECONDS:
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
    with urllib.request.urlopen(request, timeout=FF_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    rows = payload if isinstance(payload, list) else []
    with _ff_lock:
        _ff_cache[url] = (now, rows)
    return rows


def ff_stamp(raw: object) -> str:
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


def economic_calendar(lo: str, hi: str) -> list[dict[str, object]]:
    """Economic events between two YYYY-MM-DD dates, shaped like FMP's rows.

    Covers the current and next week only; that is all the feed publishes."""
    rows: list[dict[str, object]] = []
    for url in FF_WEEKS:
        try:
            rows.extend(ff_fetch(url))
        except Exception:  # noqa: BLE001 — one dead week file is not an error
            continue

    events: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = ff_stamp(row.get("date"))
        if not stamp:
            continue
        day = stamp[:10]
        if day < lo or day > hi:
            continue
        currency = str(row.get("country") or "").upper()
        events.append(
            {
                "date": stamp,
                "country": FF_COUNTRIES.get(currency, currency),
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


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # noqa: SLF001
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _day(value: object) -> str:
    stamp = getattr(value, "timestamp", None)
    if callable(stamp):
        try:
            return datetime.fromtimestamp(value.timestamp(), timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):  # NaT
            return ""
    return str(value)[:10]


def _earnings_pages(lo: str, hi: str) -> tuple[list[dict[str, object]], bool]:
    import yfinance as yf

    calendar = yf.Calendars(start=lo, end=hi)
    rows: list[dict[str, object]] = []
    for page in range(_EARNINGS_MAX_PAGES):
        frame = calendar.get_earnings_calendar(
            start=lo, end=hi, limit=_EARNINGS_PAGE, offset=page * _EARNINGS_PAGE,
            filter_most_active=False, force=True,
        )
        if frame is None or len(frame) == 0:
            return rows, False
        for symbol, row in frame.iterrows():
            date = _day(row.get("Event Start Date"))
            if not date or date < lo or date > hi:
                continue
            rows.append(
                {
                    "symbol": str(symbol),
                    "date": date,
                    "epsActual": _num(row.get("Reported EPS")),
                    "epsEstimated": _num(row.get("EPS Estimate")),
                    "revenueActual": None,
                    "revenueEstimated": None,
                    "lastUpdated": None,
                }
            )
        if len(frame) < _EARNINGS_PAGE:
            return rows, False
    return rows, True


def earnings_calendar(lo: str, hi: str) -> tuple[list[dict[str, object]], bool]:
    """US reporters between two YYYY-MM-DD dates, oldest first, and whether a
    size cap cut later rows. Yahoo publishes estimates and reported EPS, never
    revenue."""
    rows, truncated = _earnings_pages(lo, hi)
    rows.sort(key=lambda row: (str(row["date"]), str(row["symbol"])))
    return rows, truncated


async def earnings_calendar_async(lo: str, hi: str) -> tuple[list[dict[str, object]], bool]:
    return await asyncio.to_thread(earnings_calendar, lo, hi)


async def economic_calendar_async(lo: str, hi: str) -> list[dict[str, object]]:
    return await asyncio.to_thread(economic_calendar, lo, hi)
