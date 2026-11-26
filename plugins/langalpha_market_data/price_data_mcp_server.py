#!/usr/bin/env python3
"""Price Data MCP Server.

Provides normalized OHLCV time series data and short sale analytics via MCP.

Design goals:
- Small, stable tool surface (high PTC value)
- Standard agent-facing envelope (see mcp_servers/AGENT_CONTRACT.md)
- Can run in sandbox (stdio) for OSS/dev
- Can be deployed externally (http/sse) for production

Tools:
- get_stock_data: stock OHLCV
- get_asset_data: stock/commodity/crypto/forex OHLCV
- get_short_data: short interest (bi-monthly) and short volume (daily)

Symbols are resolved at the boundary through market_protocol (canonical
identity, currency, timezone); prices are returned in major currency units
(GBX/pence venues converted to pounds).
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

import asyncio
import logging
from datetime import datetime, timedelta, timezone as _tz
from types import SimpleNamespace
from typing import Any, Literal, Optional

import httpx

from data_client.fmp import close_fmp_client, get_fmp_client
from data_client.lifespan import closing_lifespan
from data_client.ginlix_data import (
    DAILY_INTERVALS,
    close_ginlix_mcp_client,
    get_ginlix_mcp_client,
)
from data_client.market_data_provider import symbol_market
from data_client.normalize import (
    listing_currency,
    minor_unit_places,
    normalize_bars,
    populated,
    scale_price,
    series_labels,
)
from data_client.ginlix_data.cn_source import GinlixDataCnSource
from data_client.ginlix_data.directory import NameDirectory
from data_client.free_fallback import free_fallback_enabled
from market_protocol import (
    InstrumentRef,
    index_families,
    is_family_index,
    market_of,
    to_canonical,
    to_legacy_api,
    to_request_spelling,
)
from market_protocol.enums import AssetClass
from market_protocol.symbology import FUTURES_MIC

try:
    from _envelope import (
        error_from_exception,
        error_from_upstream,
        make_error,
        make_response,
        normalize_interval,
    )
except ModuleNotFoundError:  # imported as a package module (tests)
    from mcp_servers._envelope import (
        error_from_exception,
        error_from_upstream,
        make_error,
        make_response,
        normalize_interval,
    )

from mcp_servers._dates import (
    FROM_TO,
    NEW_YORK,
    Bounds,
    DateBoundError,
    commodity_daily_days,
    daily_days,
    intraday_window,
    whole_daily_days,
)
from mcp_servers._schemas import (
    BAR_LABELS,
    FALLBACK_FROM,
    INCOMPLETE,
    OBJECT,
    RECORDS,
    RECORDS_BY_KEY,
    STR,
    TRUNCATED,
    described,
    envelope_schema,
    output_model,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


_ginlix = get_ginlix_mcp_client()

# Intervals served by each family. Stock/index route through the ginlix-data
# (CN daily, US) + FMP (global) fetch clients, which cover intraday + daily only.
_STOCK_INTRADAY = {"1min", "5min", "15min", "30min", "1hour", "4hour"}
_STOCK_INTERVALS = _STOCK_INTRADAY | {"1day"}
# FMP commodity/crypto/forex intraday coverage is narrower.
_ASSET_INTRADAY = {"1min", "5min", "1hour"}
_ASSET_INTERVALS = _ASSET_INTRADAY | {"1day"}

#: Calendar days of daily bars an undated CN request gets. The CN source would
#: fill two years, the window its live cache entry covers; an agent's undated
#: question is about the recent tape, and FMP's own undated window keeps the
#: other listings about as short.
_CN_DEFAULT_DAILY_DAYS = 90

#: Family indexes the US tier serves: the known families homed on a US venue.
_US_INDEX_FAMILIES = frozenset(
    ref.index_family for ref in index_families() if market_of(ref) == "us"
)


_lifespan = closing_lifespan(close_ginlix_mcp_client, close_fmp_client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_Resolved = tuple[str, str, Optional[str], Optional[str], Optional[InstrumentRef]]


def _resolve(symbol: str, asset_class: Optional[AssetClass] = None) -> _Resolved:
    """Resolve a caller symbol via the protocol registry.

    Returns ``(display, legacy, currency, timezone, ref)``: the spelling the
    tool answers with, the legacy provider spelling, ISO-4217 price currency,
    IANA timezone, and the ref itself. Falls back to the raw symbol with unknown
    currency/tz and no ref. A family index answers as ``^GSPC``, never its
    family name: a bare ``SPX`` sent back would read as a stock listing.
    """
    try:
        ref = to_canonical(symbol, asset_class=asset_class)
        return to_request_spelling(ref), to_legacy_api(ref), listing_currency(ref), ref.tz, ref
    except Exception:  # noqa: BLE001
        return symbol, symbol, None, None, None


def _resolve_interval(
    interval: str, served: set[str], *, symbol: str
) -> tuple[Optional[str], Optional[dict]]:
    """Normalize *interval* to canonical vocab, constrained to *served*.

    Returns ``(canonical, None)`` on success or ``(None, error_envelope)`` for
    unknown spellings and vocab this tool does not serve.
    """
    canonical = normalize_interval(interval)
    if canonical is None or canonical not in served:
        return None, make_error(
            "unsupported_interval",
            f"Interval {interval!r} is not supported by this tool.",
            symbol=symbol,
            interval=interval,
            supported=sorted(served),
        )
    return canonical, None


def _display_rows(
    rows: list[dict], symbol: str, *, intraday: bool, places: int = 0
) -> list[dict]:
    """Raw provider rows → ascending ``{date, open, high, low, close, volume}``.

    Timestamps are localized to *symbol*'s exchange timezone; price fields are
    shifted to major units (``places`` = 2 for GBX/pence venues). Volume is a
    share count and never scaled.
    """
    normalized = normalize_bars(rows, symbol, intraday=intraday)  # descending
    normalized.reverse()  # ascending, oldest-first (contract)
    if places:
        for row in normalized:
            for field in ("open", "high", "low", "close"):
                row[field] = scale_price(row[field], places)
    return normalized


def _series_labels(env: dict, ref: Optional[InstrumentRef], interval: str) -> dict:
    """:func:`series_labels` for an ascending envelope."""
    newest = env["data"][-1]["date"] if env["data"] else None
    return series_labels(env["source"], ref, interval, newest)


_names = NameDirectory(_ginlix.get_instruments_v2, deadline=2.0)
_cn = GinlixDataCnSource(_ginlix)


def _http_miss(exc: httpx.HTTPError) -> str:
    """The error class, and the status when there was one; never the URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__} {exc.response.status_code}"
    return type(exc).__name__


async def _fetch_stock_series(
    symbol: str,
    interval: str,
    start_date: Optional[str],
    end_date: Optional[str],
    names: Bounds = ("start_date", "end_date"),
) -> tuple[Optional[dict], Optional[dict]]:
    """Fetch stock OHLCV: ginlix-data (CN daily, then US) → FMP fallback (global).

    Returns ``(make_response_kwargs, None)`` on success or ``(None, error)``.
    The success kwargs carry canonical ``symbol``/``interval``, ``currency``,
    ``timezone``, ``data`` (ascending), the resolved ``source``, and for
    CN and HK listings the local-language ``name``, whichever source served
    the bars. *names* are the caller's names for the two bounds, which a
    refusal quotes.
    """
    # A bare ticker is the listing (COMP the stock, not the Nasdaq Composite);
    # only an index spelling (^GSPC, I:SPX, SPX.INDEX) asks for index bars.
    resolved = _resolve(symbol, asset_class=AssetClass.EQUITY)
    (name, _), (ok, err) = await asyncio.gather(
        _names.display_names(resolved[1]),
        _fetch_stock_bars(resolved, interval, start_date, end_date, names),
    )
    if ok is not None:
        ok.update(_series_labels(ok, resolved[4], ok["interval"]))
        if name:
            ok["name"] = name
    return ok, err


async def _fetch_stock_bars(
    resolved: _Resolved,
    interval: str,
    start_date: Optional[str],
    end_date: Optional[str],
    names: Bounds,
) -> tuple[Optional[dict], Optional[dict]]:
    display, legacy, currency, timezone, ref = resolved
    canonical, err = _resolve_interval(interval, _STOCK_INTERVALS, symbol=display)
    if err is not None:
        return None, err

    intraday = canonical not in DAILY_INTERVALS
    if intraday and (not start_date or not end_date):
        return None, make_error(
            "invalid_argument",
            f"{names[0]} and {names[1]} are required for intraday intervals "
            "(YYYY-MM-DD or YYYY-MM-DD HH:MM).",
            symbol=display,
            interval=canonical,
        )

    # CN A-shares, daily bars only: ginlix-data's minute history is short, so
    # CN intraday stays on the existing fallbacks.
    if intraday or symbol_market(legacy) != "cn":
        return await _fetch_chain_bars(resolved, canonical, start_date, end_date, names)

    # A caller's bad bounds are refused here, before tushare is asked, so they
    # never read as a tushare failure.
    try:
        daily_days(start_date, end_date, names)
    except DateBoundError as e:
        return None, error_from_exception(e, "FMP fetch failed.", symbol=display, interval=canonical)

    # The window runs back from the end the caller gave, else from today.
    cn_start = start_date
    if not start_date:
        try:
            end = datetime.strptime((end_date or "")[:10], "%Y-%m-%d")
        except ValueError:
            end = datetime.now(_tz.utc)
        cn_start = (end - timedelta(days=_CN_DEFAULT_DAILY_DAYS)).strftime("%Y-%m-%d")
    try:
        fetched = await _cn.get_daily(legacy, cn_start, end_date)
    except httpx.HTTPError as exc:
        # An upstream miss (a 503, a 429, a dropped connection) is expected and
        # the chain answers instead, so one line is enough.
        failed = _http_miss(exc)
        logger.warning("ginlix-data CN daily unavailable for %s: %s", display, failed)
    except Exception as exc:  # noqa: BLE001 - the chain answers instead
        failed = type(exc).__name__
        logger.warning("ginlix-data CN daily failed for %s", display, exc_info=True)
    else:
        if fetched.bars:
            return {
                "data": _display_rows(fetched.bars, legacy, intraday=False),
                "source": "tushare",
                "symbol": display,
                "interval": canonical,
                "currency": currency,
                "timezone": timezone,
                # The series' coverage flag marks sessions missing inside the
                # window (a refetch that failed), not a cap cutting either end.
                "incomplete": fetched.truncated,
            }, None
        return await _fetch_chain_bars(resolved, canonical, start_date, end_date, names)
    # A failed tushare read is not a miss: the chain's rows name it, and with
    # none the read is an error.
    ok, err = await _fetch_chain_bars(resolved, canonical, start_date, end_date, names)
    if ok is not None and ok["data"]:
        return {**ok, "fallback_from": "tushare"}, None
    if err is not None:
        return None, {**err, "detail": f"tushare failed ({failed}) and {err['detail']}"}
    return None, make_error(
        "upstream_error",
        f"tushare failed ({failed}) and FMP has no rows for this listing",
        symbol=display,
        interval=canonical,
    )


def _free():
    """The free-feed helpers, loaded on first use so a sandbox without yfinance
    still starts this server and answers from its paid feeds."""
    from data_client.yfinance.free_bars import desk_bars, free_bars, yahoo_free_symbol
    from data_client.yfinance.market_intel_source import YFinanceMarketIntelSource

    return SimpleNamespace(
        desk_bars=desk_bars,
        free_bars=free_bars,
        yahoo_free_symbol=yahoo_free_symbol,
        YFinanceMarketIntelSource=YFinanceMarketIntelSource,
    )


async def _free_else(
    answer: tuple[Optional[dict], Optional[dict]],
    resolved: _Resolved,
    canonical: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> tuple[Optional[dict], Optional[dict]]:
    """*answer*, unless the free feed has the bars the paid tiers lack.

    A refused or empty FMP read (a lapsed plan, a missing key) is answered from
    Yahoo Finance, which says so: ``source`` names it and ``fallback_from`` the
    source it stood in for. Without bars the paid answer stands, error included.
    """
    if not free_fallback_enabled():
        return answer
    display, legacy, currency, timezone, ref = resolved
    family = ref is not None and is_family_index(ref)
    try:
        free = _free()
        bars = await free.free_bars(
            legacy, canonical, start_date, end_date, is_index=family,
            yahoo=free.yahoo_free_symbol(display, "stock", is_index=family) if family else None,
        )
    except Exception as exc:  # noqa: BLE001 (the paid answer stands)
        logger.warning("free bars unavailable for %s: %s", display, type(exc).__name__)
        return answer
    # free_bars already shifted a pence venue's prices to major units.
    data = _display_rows(
        bars, legacy, intraday=canonical not in DAILY_INTERVALS, places=0
    )
    if not data:
        return answer
    return {
        "data": data,
        "source": "yfinance",
        "fallback_from": "fmp",
        "symbol": display,
        "interval": canonical,
        "currency": currency,
        "timezone": timezone,
        "truncated": False,
    }, None


async def _free_asset_rows(
    ask: str, display: str, asset_type: str, interval: str,
    from_date: Optional[str], to_date: Optional[str],
) -> tuple[list, str]:
    """A pair's, coin's or commodity's bars from the free feeds: ``(rows, source)``.

    A coin is read from Binance first (it quotes in real time) and Yahoo after;
    a rate or a commodity comes from Yahoo. ``([], "yfinance")`` when none has it.
    """
    try:
        free = _free()
        yahoo = free.yahoo_free_symbol(ask, asset_type)
        if asset_type == "crypto":
            bars = await free.desk_bars(yahoo, interval, from_date, to_date)
            if bars:
                return bars, "binance"
        bars = await free.free_bars(
            display, interval, from_date, to_date, asset_type=asset_type, yahoo=yahoo
        )
        return bars, "yfinance"
    except Exception as exc:  # noqa: BLE001 (the paid answer stands)
        logger.warning("free bars unavailable for %s: %s", display, type(exc).__name__)
        return [], "yfinance"


async def _free_short_data(
    display: str,
    legacy: str,
    timezone: Optional[str],
    data_type: str,
    from_date: Optional[str],
    to_date: Optional[str],
    limit: int,
) -> dict:
    """Short interest from Yahoo Finance and short volume from FINRA's daily file.

    The record shapes and newest-first order match the ginlix-data read; a
    section that fails is named in ``errors`` by the failure's class alone.
    """
    try:
        intel = _free().YFinanceMarketIntelSource()
    except Exception as exc:  # noqa: BLE001
        return make_error(
            "client_unavailable",
            f"Short data needs ginlix-data or yfinance ({type(exc).__name__}).",
            symbol=display,
            data_type=data_type,
        )
    wanted = ("short_interest", "short_volume") if data_type == "both" else (data_type,)
    data: dict[str, list] = {}
    errors: dict[str, str] = {}
    for section in wanted:
        key = "settlement_date" if section == "short_interest" else "date"
        try:
            if section == "short_interest":
                rows = await intel.get_short_interest(
                    legacy, limit=max(limit, 1), sort=f"{key}.desc"
                )
            else:
                rows = await intel.get_short_volume(legacy, limit=500, sort=f"{key}.desc")
        except Exception as exc:  # noqa: BLE001 (the other section may answer)
            errors[section] = f"{type(exc).__name__}: {section} unavailable"
            continue
        rows = sorted(
            (row for row in rows if isinstance(row, dict) and row.get(key)),
            key=lambda row: str(row[key]),
            reverse=True,
        )
        if from_date:
            rows = [row for row in rows if str(row[key])[:10] >= from_date[:10]]
        if to_date:
            rows = [row for row in rows if str(row[key])[:10] <= to_date[:10]]
        if rows:
            data[section] = rows[: max(limit, 1)]

    if not data:
        return make_error(
            "upstream_error",
            "; ".join(errors.values()) or "No short data answered.",
            symbol=display,
            data_type=data_type,
        )
    extra: dict[str, Any] = {"data_type": data_type}
    if errors:
        extra["errors"] = errors
    return make_response(
        data, source="yfinance", symbol=display, timezone=timezone, **extra
    )


async def _fetch_chain_bars(
    resolved: _Resolved,
    canonical: str,
    start_date: Optional[str],
    end_date: Optional[str],
    names: Bounds,
) -> tuple[Optional[dict], Optional[dict]]:
    """Bars from ginlix-data's US tier, else FMP, in the shape of ``_fetch_stock_bars``."""
    display, legacy, currency, timezone, ref = resolved
    intraday = canonical not in DAILY_INTERVALS

    # A daily window is read here, in the caller's words, before the US tier
    # is asked: that tier reads only a whole window, so an undated read gets
    # the one FMP would serve it and reaches the same source as a dated one.
    if not intraday:
        try:
            start_date, end_date = whole_daily_days(start_date, end_date, names)
        except DateBoundError as e:
            return None, error_from_exception(
                e, "FMP fetch failed.", symbol=display, interval=canonical
            )

    # The legacy spelling of a family index is bare (GSPC), which every vendor
    # reads as a stock ticker: FMP takes the request spelling (^GSPC), Massive
    # its I:SPX index ticker.
    family = ref is not None and is_family_index(ref)
    fmp_symbol = display

    # ginlix-data's US tier: US equities, and US family indexes on the index
    # market. A foreign family index has no tier and goes straight to FMP, as
    # does one the protocol does not know, which it homes on a US venue only
    # by default.
    if not family:
        ginlix_result = await _ginlix.fetch_stock_data(legacy, canonical, start_date, end_date)
    elif ref.index_family in _US_INDEX_FAMILIES:
        ginlix_result = await _ginlix.fetch_stock_data(
            f"I:{ref.index_family}", canonical, start_date, end_date, market="index"
        )
    else:
        ginlix_result = None
    # What this read answers when FMP has no rows for it.
    miss: Optional[tuple[Optional[dict], Optional[dict]]] = None
    if isinstance(ginlix_result, dict):
        status = ginlix_result.get("status")
        err = error_from_upstream(
            ginlix_result.get("error", "ginlix-data request failed"),
            status=status,
            symbol=display,
            interval=canonical,
        )
        if status not in (401, 403):
            return None, err
        # A refused credential says nothing about the request, so FMP may still
        # answer it. The refusal stays the error unless FMP returns rows.
        logger.warning("ginlix-data refused the credential for %s: %s", display, err["detail"])
        miss = None, err
    elif ginlix_result is not None:
        # Already-normalized display rows, descending → flip to ascending.
        tier = {
            "data": list(reversed(ginlix_result.bars)),
            "source": "ginlix-data",
            "symbol": display,
            "interval": canonical,
            "currency": currency,
            "timezone": timezone,
            "truncated": ginlix_result.truncated,
        }
        if populated(ginlix_result.bars):
            return tier, None
        # No bars is a miss too: Massive answers a range older than its history
        # with none (AAPL in 1995), which FMP still holds. The empty answer
        # stands unless FMP returns rows.
        miss = tier, None

    # FMP fallback (all markets; converts GBX/pence to pounds).
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return await _free_else(miss or (None, make_error(
            "client_unavailable",
            "FMP client unavailable (FMP_API_KEY not configured).",
            symbol=display,
        )), resolved, canonical, start_date, end_date)

    places = minor_unit_places(legacy)
    window = None
    try:
        # An intraday bound FMP would drop, or a reversed window, is refused
        # here, as the hosted tools refuse it (mcp_servers/_dates.py). The
        # ginlix-data tier above sends both on to this read rather than
        # answering them.
        if canonical in DAILY_INTERVALS:
            rows = await client.get_stock_price(fmp_symbol, from_date=start_date, to_date=end_date)
        else:
            window = intraday_window(start_date, end_date, timezone, names=names)
            rows = await client.get_intraday_chart(
                fmp_symbol, canonical, from_date=window.from_day, to_date=window.to_day
            )
    except Exception as e:  # noqa: BLE001
        return await _free_else(miss or (None, error_from_exception(
            e, "FMP fetch failed.", symbol=display, interval=canonical
        )), resolved, canonical, start_date, end_date)
    if not isinstance(rows or [], list):
        # FMP answers some failures with a 200 and an error object, not rows.
        return await _free_else(miss or (None, make_error(
            "upstream_error", "FMP fetch failed.", symbol=display, interval=canonical
        )), resolved, canonical, start_date, end_date)
    truncated = getattr(rows, "truncated", False)
    if window is not None:
        rows = window.rows(rows or [])

    data = _display_rows(rows or [], legacy, intraday=intraday, places=places)
    if not data:
        # An empty read stands unless the free feed holds the bars.
        empty = {
            "data": data, "source": "fmp", "symbol": display, "interval": canonical,
            "currency": currency, "timezone": timezone, "truncated": truncated,
        }
        return await _free_else(miss or (empty, None), resolved, canonical, start_date, end_date)
    return {
        "data": data,
        "source": "fmp",
        "symbol": display,
        "interval": canonical,
        "currency": currency,
        "timezone": timezone,
        "truncated": truncated,
    }, None


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------

mcp = MCPServer("PriceDataMCP", lifespan=_lifespan)


_NAME_ECHO = described(STR, "Local-language company name; CN and HK listings only, omitted elsewhere.")

_OUT_GET_STOCK_DATA = output_model(
    "GetStockDataOut",
    envelope_schema(
        RECORDS,
        frame=("symbol", "interval", "currency", "timezone"),
        echo={
            **BAR_LABELS, "name": _NAME_ECHO, "truncated": TRUNCATED, "incomplete": INCOMPLETE,
            "fallback_from": FALLBACK_FROM,
        },
    ),
)


@mcp.tool()
async def get_stock_data(
    symbol: str,
    interval: str = "1day",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> _OUT_GET_STOCK_DATA:
    """Fetch OHLCV price bars for a stock.

    Intervals: 1min|5min|15min|30min|1hour|4hour|1day. Intraday needs start_date
    and end_date.

    Args:
        symbol: Ticker — US "AAPL", HK "0700.HK", A-share "600519.SH", LSE "VOD.L".
        interval: See above.
        start_date: "YYYY-MM-DD[ HH:MM]".
        end_date: Same format.

    Returns:
        dict: {symbol, interval, currency?, timezone, count, data, source,
        price_treatment, name?, in_progress?, truncated?, incomplete?}. data:
        list of {date, open, high, low, close, volume} ascending (oldest first);
        date exchange-local "YYYY-MM-DD[ HH:MM:SS]"; prices in `currency` major
        units. price_treatment: raw|split_adjusted|dividend_adjusted.
        in_progress: date of a forming last bar; some daily feeds omit it.
        truncated: true if a size cap cut the oldest bars. incomplete: true if
        bars in the window are missing. On error: {error: <code>, detail}.
    """
    env, err = await _fetch_stock_series(symbol, interval, start_date, end_date)
    if err is not None:
        return err
    return make_response(**env)


_OUT_GET_ASSET_DATA = output_model(
    "GetAssetDataOut",
    envelope_schema(
        RECORDS,
        frame=("symbol", "interval", "currency", "timezone"),
        echo={
            "asset_type": STR, **BAR_LABELS, "name": _NAME_ECHO,
            "truncated": TRUNCATED, "incomplete": INCOMPLETE, "fallback_from": FALLBACK_FROM,
        },
    ),
)


@mcp.tool()
async def get_asset_data(
    symbol: str,
    asset_type: str,
    interval: str = "1day",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> _OUT_GET_ASSET_DATA:
    """Fetch OHLCV bars for a stock, commodity, crypto, or forex pair.

    Intervals: 1min|5min|1hour|1day; intraday needs dates.

    Args:
        symbol: Commodity "GCUSD", crypto "BTCUSD", forex "EURUSD".
        asset_type: stock|commodity|crypto|forex.
        interval: See above.
        from_date: "YYYY-MM-DD[ HH:MM]".
        to_date: Same format.

    Returns:
        dict: {symbol, asset_type, interval, currency?, timezone, count, data,
        source, price_treatment, name?, in_progress?, truncated?, incomplete?}.
        data: list of {date, open, high, low, close, volume} ascending; date
        exchange-local "YYYY-MM-DD[ HH:MM:SS]"; prices in `currency` major
        units. price_treatment: raw|split_adjusted|dividend_adjusted.
        in_progress: date of a forming last bar. truncated: true if a size cap
        cut the oldest bars. incomplete: true if bars in the window are missing.
        On error: {error: <code>, detail}.
    """
    at = asset_type.lower().strip()
    if at not in {"stock", "commodity", "crypto", "forex"}:
        return make_error(
            "invalid_argument",
            f"Invalid asset_type {asset_type!r}.",
            supported=["stock", "commodity", "crypto", "forex"],
        )

    if at == "stock":
        env, err = await _fetch_stock_series(symbol, interval, from_date, to_date, FROM_TO)
        if err is not None:
            return err
        env["asset_type"] = at
        return make_response(**env)

    # commodity / crypto / forex — FMP direct (the provider chain and ginlix
    # cover equities/indexes only). Currency + timezone from canonical identity.
    hint = {"crypto": AssetClass.CRYPTO, "forex": AssetClass.FX}.get(at)
    display, _legacy, currency, timezone, ref = _resolve(symbol, asset_class=hint)
    # FMP spells a pair as one run (BTCUSD, EURUSD), however the caller did,
    # so the BTC-USD this tool answers with can be sent back.
    pair = ref is not None and ref.asset_class in (AssetClass.CRYPTO, AssetClass.FX)
    fmp_symbol = ref.symbol.replace("-", "") if pair else symbol
    if at == "commodity" and ref is not None:
        # A commodity keeps CME Globex hours, settling at its 16:00 Chicago halt,
        # and adjusts for nothing. FMP labels it in New York time, the zone the
        # ref keeps.
        ref = ref.model_copy(update={"mic": FUTURES_MIC, "calendar_id": FUTURES_MIC})
    canonical, err = _resolve_interval(interval, _ASSET_INTERVALS, symbol=display)
    if err is not None:
        return err

    intraday = canonical not in DAILY_INTERVALS
    if intraday and (not from_date or not to_date):
        return make_error(
            "invalid_argument",
            "from_date and to_date are required for intraday intervals "
            "(YYYY-MM-DD or YYYY-MM-DD HH:MM).",
            symbol=display,
            interval=canonical,
        )

    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        client = None
        if not free_fallback_enabled():
            return make_error(
                "client_unavailable",
                "FMP client unavailable (FMP_API_KEY not configured).",
                symbol=display,
            )

    rows: Any = None
    window = None
    truncated = False
    paid_error: Optional[dict] = None
    if client is None:
        paid_error = make_error(
            "client_unavailable",
            "FMP client unavailable (FMP_API_KEY not configured).",
            symbol=display,
        )
    else:
        try:
            # Bounds are read as the hosted tools read them (mcp_servers/_dates.py).
            if canonical in DAILY_INTERVALS:
                if at == "commodity":
                    lo, hi = commodity_daily_days(from_date, to_date)
                    rows = await client.get_commodity_price(symbol, from_date=lo, to_date=hi)
                else:
                    lo, hi = daily_days(from_date, to_date)
                    if at == "crypto":
                        rows = await client.get_crypto_price(fmp_symbol, from_date=lo, to_date=hi)
                    else:
                        rows = await client.get_forex_price(fmp_symbol, from_date=lo, to_date=hi)
            elif at == "commodity":
                # FMP labels a commodity in New York time, the zone its envelope
                # states, so its labels stand.
                window = intraday_window(from_date, to_date, NEW_YORK)
                rows = await client.get_commodity_intraday_chart(
                    symbol, canonical, from_date=window.from_day, to_date=window.to_day
                )
            else:
                # FMP labels a pair in New York time too, but its envelope states
                # UTC: the window is read there and the labels rewritten into it.
                window = intraday_window(from_date, to_date, timezone, labels=NEW_YORK)
                if at == "crypto":
                    rows = await client.get_crypto_intraday_chart(
                        fmp_symbol, canonical, from_date=window.from_day, to_date=window.to_day
                    )
                else:
                    rows = await client.get_forex_intraday_chart(
                        fmp_symbol, canonical, from_date=window.from_day, to_date=window.to_day
                    )
        except Exception as e:  # noqa: BLE001
            if not free_fallback_enabled():
                return error_from_exception(
                    e, "FMP fetch failed.", symbol=display, interval=canonical
                )
            # A refused plan is not the caller's mistake: the free feeds may
            # still answer, and this error stands when they do not.
            logger.warning("FMP fetch failed for %s; trying the free feeds", display)
            paid_error = error_from_exception(
                e, "FMP fetch failed.", symbol=display, interval=canonical
            )
        else:
            truncated = getattr(rows, "truncated", False)
            if window is not None and isinstance(rows, list):
                rows = window.rows(rows)

    source, fallback_from = "fmp", None
    if not rows and free_fallback_enabled():
        # Binance for a coin, Yahoo for the rest: the vendor spellings differ,
        # so each feed is asked in its own.
        free_rows, source = await _free_asset_rows(
            fmp_symbol if pair else symbol, display, at, canonical, from_date, to_date
        )
        if free_rows:
            rows, fallback_from = free_rows, "fmp"
    if not rows and paid_error is not None:
        return paid_error

    env = {
        "data": _display_rows(rows or [], symbol, intraday=intraday),
        "source": source,
        "symbol": display,
        "interval": canonical,
        "currency": currency,
        "timezone": timezone,
        "truncated": bool(rows) and truncated,
        "asset_type": at,
    }
    if fallback_from:
        env["fallback_from"] = fallback_from

    return make_response(**env, **_series_labels(env, ref, canonical))


_OUT_GET_SHORT_DATA = output_model(
    "GetShortDataOut",
    envelope_schema(
        RECORDS_BY_KEY,
        frame=("symbol", "timezone"),
        echo={
            "data_type": STR,
            "errors": described(
                OBJECT, "Per-section upstream errors on partial success."
            ),
        },
    ),
)


@mcp.tool()
async def get_short_data(
    symbol: str,
    data_type: Literal["short_interest", "short_volume", "both"] = "both",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 20,
) -> _OUT_GET_SHORT_DATA:
    """Fetch short interest and/or short volume for a US stock.

    Short interest is FINRA bi-monthly (settlement_date); short volume is daily
    off-exchange (date).

    Args:
        symbol: Stock ticker, e.g. "AAPL".
        data_type: "short_interest", "short_volume", or "both" (default).
        from_date: "YYYY-MM-DD" start filter (optional).
        to_date: "YYYY-MM-DD" end filter (optional).
        limit: Max records per section (default 20, max 50000).

    Returns:
        dict: {symbol, timezone, count, data, source, data_type}. data is a dict
        of {short_interest, short_volume} lists (requested sections only), newest
        first. Record fields are vendor-native (e.g. settlement_date);
        short_volume_ratio is a percent (43.3 = 43.3%). Partial failures appear
        in an `errors` map. On error: {error: <code>, detail}.
    """
    display, legacy, _currency, timezone, ref = _resolve(symbol, asset_class=AssetClass.EQUITY)

    # FINRA's figures cover US listings only, so another market's listing would
    # read as one with no short activity. A venue the protocol does not know stays.
    if ref is not None and market_of(ref) not in ("us", "other"):
        return make_error(
            "invalid_argument",
            f"Short data covers US listings only; {display} is not one.",
            symbol=display,
            data_type=data_type,
        )

    if not await _ginlix.ensure():
        if free_fallback_enabled():
            return await _free_short_data(
                display, legacy, timezone, data_type, from_date, to_date, limit
            )
        return make_error(
            "client_unavailable",
            "Short data requires ginlix-data (not configured).",
            symbol=display,
        )

    raw = await _ginlix.fetch_short_data(
        legacy, data_type=data_type, from_date=from_date, to_date=to_date, limit=limit,
    )

    data: dict[str, list] = {}
    errors: dict[str, str] = {}
    for section in ("short_interest", "short_volume"):
        if section in raw:
            data[section] = raw[section]
        if f"{section}_error" in raw:
            errors[section] = raw[f"{section}_error"]

    if not data and free_fallback_enabled():
        return await _free_short_data(
            display, legacy, timezone, data_type, from_date, to_date, limit
        )

    if not data:
        return make_error(
            "upstream_error",
            "; ".join(errors.values()) or "No short data returned.",
            symbol=display,
            data_type=data_type,
        )

    extra: dict[str, Any] = {"data_type": data_type}
    if errors:
        extra["errors"] = errors
    return make_response(
        data,
        source="ginlix-data",
        symbol=display,
        timezone=timezone,
        **extra,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
