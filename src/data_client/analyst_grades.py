"""Analyst grade actions and published price targets: FMP's rows, else Yahoo's.

``get_stock_grades`` and ``get_price_target_news`` have no entry in the
financial-source protocol, so their callers ask FMP directly. This asks FMP
first and, when FMP has no key, is down or has nothing, builds the same rows
from Yahoo Finance.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime
from typing import Any

from .free_fallback import free_fallback_enabled
from .yfinance.yahoo import yahoo_ticker

logger = logging.getLogger(__name__)

Row = dict[str, Any]

# How Yahoo names a grade action.
_ACTIONS = {"up": "upgrades", "down": "downgrades", "init": "initiates", "main": "maintains"}


def _value(cell: Any) -> Any:
    return None if cell is None or (isinstance(cell, float) and math.isnan(cell)) else cell


def _day(cell: Any) -> str | None:
    """A date cell as ``YYYY-MM-DD``; ``to_dict`` would drop the frame's index."""
    if cell is None:
        return None
    if isinstance(cell, str):
        return cell[:10] if cell else None
    to_pydatetime = getattr(cell, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            cell = to_pydatetime()
        except (TypeError, ValueError):
            return None
    if isinstance(cell, (datetime, date)):
        try:
            return cell.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None
    return None


def _rows(frame: Any, limit: int, *, dated_by: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """*frame*'s first *limit* rows, newest first, as plain dicts keyed by lower-cased column.

    ``dated_by`` names the index a date is read from: Yahoo keeps the grade
    date as the index, not a column.
    """
    if frame is None or not hasattr(frame, "to_dict") or frame.empty:
        return []
    order = frame
    if hasattr(order, "sort_index"):
        order = order.sort_index(ascending=False, kind="stable")
    if dated_by and hasattr(order, "reset_index"):
        order = order.reset_index()
    out = []
    for row in order.head(limit).to_dict(orient="records"):
        item = {str(key).lower(): _value(value) for key, value in row.items()}
        for name in dated_by:
            if name in row:
                item["date"] = _day(row[name])
                break
        out.append(item)
    return out


def yahoo_stock_grades(symbol: str, limit: int) -> list[Row]:
    """Grade actions in FMP's ``get_stock_grades`` row shape."""
    try:
        frame = yahoo_ticker(symbol).upgrades_downgrades
    except Exception as exc:  # noqa: BLE001 (Yahoo is a stand-in; the caller reports an empty list)
        logger.warning("analyst_grades.yahoo | symbol=%s error=%s", symbol, type(exc).__name__)
        return []
    out = []
    for row in _rows(frame, limit, dated_by=("GradeDate", "date", "index")):
        action = str(row.get("action") or "").lower()
        out.append(
            {
                "symbol": symbol,
                "date": row.get("date"),
                "gradingCompany": row.get("firm"),
                "previousGrade": row.get("fromgrade"),
                "newGrade": row.get("tograde"),
                "action": _ACTIONS.get(action, row.get("action")),
            }
        )
    return out


def yahoo_price_target_news(symbol: str, limit: int) -> list[Row]:
    """The consensus target in FMP's ``get_price_target_news`` row shape.

    Yahoo publishes one current consensus, not a per-analyst history, so this
    is a single row: dated today (the day the consensus was read), no firm or
    news attached.
    """
    try:
        targets = yahoo_ticker(symbol).analyst_price_targets
    except Exception as exc:  # noqa: BLE001 (Yahoo is a stand-in; the caller reports an empty list)
        logger.warning("analyst_grades.yahoo_targets | symbol=%s error=%s", symbol, type(exc).__name__)
        return []
    mean = _value(targets.get("mean")) if isinstance(targets, dict) else None
    if mean is None:
        return []
    return [
        {
            "symbol": symbol,
            "publishedDate": date.today().isoformat(),
            "analystCompany": "Consensus",
            "adjPriceTarget": mean,
            "priceWhenPosted": None,
            "newsTitle": None,
            "newsURL": None,
        }
    ][:max(int(limit), 0)]


_YAHOO = {"get_stock_grades": yahoo_stock_grades, "get_price_target_news": yahoo_price_target_news}


async def fmp_else_yahoo(
    method: str, symbol: str, limit: int, request: Any = None
) -> list[Row]:
    """``method``'s rows from FMP; from Yahoo when FMP fails or has nothing.

    ``request`` is the caller's own FMP read (``request(method, symbol,
    limit=...)``), so a caller that tests patch it keeps working; None reads
    the FMP client here. Yahoo answers only while the free-fallback switch is
    on, and never hides an FMP outage: with Yahoo empty too, FMP's failure
    is the one raised.
    """
    fallback = _YAHOO[method]
    failure = None
    try:
        if request is None:
            from .fmp import get_fmp_client

            client = await get_fmp_client()
            rows = await getattr(client, method)(symbol, limit=limit)
        else:
            rows = await request(method, symbol, limit=limit)
    except Exception as exc:  # noqa: BLE001 (no key, quota, outage: Yahoo stands in)
        logger.warning("analyst_grades.fmp | method=%s error=%s", method, type(exc).__name__)
        failure = exc
    else:
        if rows:
            return rows
    if not free_fallback_enabled():
        if failure is not None:
            raise failure
        return []
    try:
        rows = await asyncio.to_thread(fallback, symbol, limit)
    except Exception:  # noqa: BLE001 (FMP's failure is the one worth reporting)
        if failure is None:
            raise
    else:
        if rows:
            return rows
    if failure is not None:
        raise failure
    return []
