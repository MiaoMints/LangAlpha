"""Calendar endpoints — economic releases and earnings announcements."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.data_client.fmp.fmp_client import FMPClient
from src.data_client.free_calendar import economic_calendar_async, earnings_calendar_async
from src.data_client.free_fallback import free_fallback_enabled
from src.server.models.calendar import (
    EarningsCalendarResponse,
    EarningsEvent,
    EconomicCalendarResponse,
    EconomicEvent,
)
from src.server.services.cache.earnings_cache_service import EarningsCacheService
from src.server.dependencies.principal import DataReadUserId

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/calendar", tags=["Calendar"])

_earnings_cache = EarningsCacheService()


async def _spare(source, lo: str, hi: str) -> list[dict] | None:
    """Keyless rows when the switch is on and the stand-in answers, else None."""
    if not free_fallback_enabled():
        return None
    try:
        if source == "earnings":
            rows, _truncated = await earnings_calendar_async(lo, hi)
        else:
            rows = await economic_calendar_async(lo, hi)
    except Exception as exc:  # noqa: BLE001 — FMP's failure is the one reported
        logger.warning("%s calendar stand-in for %s..%s failed: %s", source, lo, hi, type(exc).__name__)
        return None
    return rows or None


def _default_dates(
    from_date: Optional[str], to_date: Optional[str]
) -> tuple[str, str]:
    """Fill in missing dates with today and the week after the start; a date given
    must be an ISO day, and the range must not end before it starts."""
    try:
        first = date.fromisoformat(from_date) if from_date else date.today()
        last = date.fromisoformat(to_date) if to_date else first + timedelta(days=7)
    except ValueError:
        raise HTTPException(status_code=422, detail="from and to must be YYYY-MM-DD") from None
    from_date, to_date = first.isoformat(), last.isoformat()
    if first > last:
        raise HTTPException(status_code=422, detail="from is after to")
    return from_date, to_date


@router.get("/economic", response_model=EconomicCalendarResponse)
async def get_economic_calendar(
    user_id: DataReadUserId,
    from_date: Optional[str] = Query(
        None, alias="from", description="Start date (YYYY-MM-DD). Defaults to today."
    ),
    to_date: Optional[str] = Query(
        None, alias="to", description="End date (YYYY-MM-DD). Defaults to 7 days after from."
    ),
) -> EconomicCalendarResponse:
    """Get upcoming and past economic data releases (GDP, CPI, etc.)."""
    from_date, to_date = _default_dates(from_date, to_date)

    try:
        fmp_client = FMPClient()
    except (ValueError, ImportError):
        # FMP unavailable: the keyless calendar answers when the switch is on
        spare = await _spare("economic", from_date, to_date)
        return _economic_response(spare, "forexfactory" if spare else "fmp")

    try:
        try:
            raw = await fmp_client.get_economic_calendar(
                from_date=from_date, to_date=to_date
            )
            spare = await _spare("economic", from_date, to_date) if not raw else None
            if spare is not None:
                return _economic_response(spare, "forexfactory")
            return _economic_response(raw or [], "fmp")
        finally:
            await fmp_client.close()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching economic calendar: %s", e)
        spare = await _spare("economic", from_date, to_date)
        if spare is not None:
            return _economic_response(spare, "forexfactory")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/earnings", response_model=EarningsCalendarResponse)
async def get_earnings_calendar(
    user_id: DataReadUserId,
    from_date: Optional[str] = Query(
        None, alias="from", description="Start date (YYYY-MM-DD). Defaults to today."
    ),
    to_date: Optional[str] = Query(
        None, alias="to", description="End date (YYYY-MM-DD). Defaults to 7 days after from."
    ),
) -> EarningsCalendarResponse:
    """Get upcoming and past earnings announcements with EPS and revenue data."""
    from_date, to_date = _default_dates(from_date, to_date)

    # Check cache
    cached = await _earnings_cache.get(from_date, to_date)
    if cached is not None:
        events = [EarningsEvent(**item) for item in cached]
        return EarningsCalendarResponse(data=events, count=len(events))

    try:
        fmp_client = FMPClient()
    except (ValueError, ImportError):
        # FMP unavailable: the keyless calendar answers when the switch is on
        spare = await _spare("earnings", from_date, to_date)
        return _earnings_response(spare, "yfinance" if spare else "fmp")

    try:
        try:
            raw = await fmp_client.get_earnings_calendar_by_date(
                from_date=from_date, to_date=to_date
            )
            items = raw or []
            if not items:
                spare = await _spare("earnings", from_date, to_date)
                if spare is not None:
                    return _earnings_response(spare, "yfinance")
            events = [EarningsEvent(**item) for item in items]

            # Populate cache; a cut read is served but not kept
            if not getattr(raw, "truncated", False):
                await _earnings_cache.set(items, from_date, to_date)

            return EarningsCalendarResponse(data=events, count=len(events))
        finally:
            await fmp_client.close()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching earnings calendar: %s", e)
        spare = await _spare("earnings", from_date, to_date)
        if spare is not None:
            return _earnings_response(spare, "yfinance")
        raise HTTPException(status_code=500, detail=str(e))


def _economic_response(rows: list | None, source: str) -> EconomicCalendarResponse:
    events = [EconomicEvent(**item) for item in rows or []]
    return EconomicCalendarResponse(data=events, count=len(events), source=source)


def _earnings_response(rows: list | None, source: str) -> EarningsCalendarResponse:
    events = [EarningsEvent(**item) for item in rows or []]
    return EarningsCalendarResponse(data=events, count=len(events), source=source)
