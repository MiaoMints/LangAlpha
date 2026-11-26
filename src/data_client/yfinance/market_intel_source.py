"""A free stand-in for ginlix-data: options, short data and movers.

Options chains and contract quotes come from Yahoo Finance, short interest and
float from the ticker's profile, and short volume from FINRA's public daily
files. Every call runs its blocking work in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import yfinance as yf

from ..base import IntervalNotSupported, MarketIntelSource, MoverRow
from .yahoo import yahoo_symbol

logger = logging.getLogger(__name__)

FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{yyyymmdd}.txt"
_FINRA_LOOKBACK_DAYS = 7
_FINRA_CACHE_TTL = 3600.0
http_get = httpx.get
_finra_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_finra_lock = threading.Lock()

# Exchange bar interval for each served interval; ``4hour`` is built from ``1hour``.
_INTERVALS = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "1hour": "1h",
    "1day": "1d",
    "1week": "1wk",
    "1month": "1mo",
}
_MOVERS = {"gainers": "day_gainers", "losers": "day_losers", "active": "most_actives", "actives": "most_actives"}
_OCC = re.compile(r"^(?P<root>.{1,6}?)(?P<expiry>\d{6})(?P<kind>[CP])(?P<strike>\d{8})$")
_NON_US = re.compile(r"\.(HK|SH|SZ|SS|L|T|TO|AX|NE|PA|DE|SW|MI|KS|TW|BK|KL|SG|JO|SA|IS|MU|ST|CO|HE|LS|BR|MX|AR)$", re.I)


def _num(value: Any) -> float | None:
    """A finite number, else ``None`` (Yahoo pads what it does not know with NaN)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _kind(contract_type: str | None) -> set[str]:
    if contract_type in (None, "", "all"):
        return {"call", "put"}
    names = {"calls": "call", "puts": "put"}
    return {names.get(str(contract_type).lower(), str(contract_type).lower())}


def _chain_sync(underlying: str, **filters: Any) -> dict[str, Any]:
    tk = yf.Ticker(yahoo_symbol(underlying))
    expiries = [str(expiry) for expiry in (tk.options or [])]
    expiries.sort()
    if filters.get("expiration_date_gte"):
        expiries = [expiry for expiry in expiries if expiry >= str(filters["expiration_date_gte"])]
    if filters.get("expiration_date_lte"):
        expiries = [expiry for expiry in expiries if expiry <= str(filters["expiration_date_lte"])]
    kinds = _kind(filters.get("contract_type"))
    low, high = filters.get("strike_price_gte"), filters.get("strike_price_lte")
    limit = filters.get("limit")
    limit = int(limit) if limit is not None else None

    rows: list[dict[str, Any]] = []
    truncated = False
    pending = [
        (expiry, kind)
        for expiry in expiries
        for kind in ("call", "put")
        if kind in kinds
    ]
    # Nearest expiries first; a caller that gave a limit is answered without
    # downloading every chain.
    pending.sort(key=lambda item: (item[0], item[1]))
    for index, (expiry, kind) in enumerate(pending):
        chain = tk.option_chain(expiry)
        frame = chain.calls if kind == "call" else chain.puts
        for row in frame.to_dict("records"):
            strike = _num(row.get("strike"))
            if strike is None:
                continue
            if low is not None and strike < float(low):
                continue
            if high is not None and strike > float(high):
                continue
            rows.append(
                {
                    "ticker": f"O:{row.get('contractSymbol', '')}",
                    "underlying_ticker": str(underlying).upper(),
                    "contract_type": kind,
                    "expiration_date": expiry,
                    "strike_price": strike,
                    "shares_per_contract": 100,
                    "primary_exchange": "OPRA",
                }
            )
        if index + 1 < len(pending) and limit is not None and len(rows) > limit:
            truncated = True
            break
    rows.sort(key=lambda row: (row["expiration_date"], row["strike_price"], row["contract_type"]))
    if limit is not None and len(rows) > limit:
        rows, truncated = rows[:limit], True
    return {"results": rows, "truncated": truncated, "next_cursor": None}


def _iso_expiry(digits: str) -> str:
    # An OCC code carries YYMMDD; Yahoo answers only the dashed form.
    return f"20{digits[:2]}-{digits[2:4]}-{digits[4:6]}"


def _snapshot_sync(tickers: list[str]) -> list[dict[str, Any]]:
    by_root: dict[str, dict[str, list[tuple[str, dict[str, Any]]]]] = {}
    for occ in tickers:
        match = _OCC.match(occ.removeprefix("O:").upper())
        if match is None:
            continue
        by_root.setdefault(match["root"], {}).setdefault(_iso_expiry(match["expiry"]), []).append((occ, {}))
    prices: dict[str, float | None] = {}
    for root, expiries in by_root.items():
        tk = yf.Ticker(root)
        try:
            prices[root] = _num(tk.fast_info.get("lastPrice", tk.fast_info.get("last_price")))
        except Exception as exc:  # noqa: BLE001 (a quote is optional context)
            logger.warning("yfinance.options.underlying_price.failed | error=%s", type(exc).__name__)
            prices[root] = None
        for expiry, wanted in expiries.items():
            try:
                chain = tk.option_chain(expiry)
            except Exception as exc:  # noqa: BLE001 (each contract still gets a row)
                logger.warning("yfinance.options.chain.failed | error=%s", type(exc).__name__)
                continue
            found = {
                str(row.get("contractSymbol", "")): row
                for frame in (chain.calls, chain.puts)
                for row in frame.to_dict("records")
            }
            for index, (occ, _) in enumerate(wanted):
                row = found.get(occ.removeprefix("O:").upper())
                if row is not None:
                    wanted[index] = (occ, row)

    rows: list[dict[str, Any]] = []
    for occ in tickers:
        match = _OCC.match(occ.removeprefix("O:").upper())
        row = _contract_row(occ, match)
        if match is None:
            rows.append(row)
            continue
        row.update(
            _quoted_contract(
                occ,
                match,
                by_root[match["root"]][_iso_expiry(match["expiry"])],
                prices.get(match["root"]),
            )
        )
        rows.append(row)
    return rows


def _contract_row(occ: str, match: re.Match[str] | None) -> dict[str, Any]:
    ticker = occ if occ.startswith("O:") else f"O:{occ}"
    root = match["root"] if match else None
    return {
        "ticker": ticker,
        "name": None,
        "market_status": None,
        "session": {
            "close": None,
            "change": None,
            "change_percent": None,
            "volume": None,
            "timeframe": "DELAYED",
            "last_updated": None,
            "stale": None,
        },
        "greeks": None,
        "implied_volatility": None,
        "open_interest": None,
        "break_even_price": None,
        "underlying_asset": {"ticker": root, "price": None},
        "last_quote": {"bid": None, "ask": None, "mid": None},
        "last_trade": None,
    }


def _quoted_contract(
    occ: str,
    match: re.Match[str],
    wanted: list[tuple[str, dict[str, Any]]],
    underlying_price: float | None = None,
) -> dict[str, Any]:
    row = next((quote for name, quote in wanted if name == occ), {})
    strike = int(match["strike"]) / 1000.0
    last = _num(row.get("lastPrice"))
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    break_even = None
    if last is not None:
        break_even = strike + last if match["kind"] == "C" else strike - last
    return {
        "session": {
            "close": last,
            "change": _num(row.get("change")),
            "change_percent": _num(row.get("percentChange")),
            "volume": _num(row.get("volume")),
            "timeframe": "DELAYED",
            # Unix ns, the stamp snapshot_row formats and dates a session by.
            "last_updated": _unix_ns(row.get("lastTradeDate")),
            "stale": None,
        },
        "implied_volatility": _num(row.get("impliedVolatility")),
        "open_interest": _num(row.get("openInterest")),
        "break_even_price": break_even,
        "underlying_asset": {"ticker": match["root"], "price": underlying_price},
        "last_quote": {"bid": bid, "ask": ask, "mid": mid},
    }


def _unix_ns(cell: Any) -> int | None:
    """A time cell (Timestamp, datetime, epoch seconds) as Unix ns."""
    if cell is None:
        return None
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        seconds = float(cell)
        if not math.isfinite(seconds):
            return None
        seconds = seconds / 1000 if seconds > 1e11 else seconds
        return int(seconds * 1e9)
    to_pydatetime = getattr(cell, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            cell = to_pydatetime()
        except (TypeError, ValueError):
            return None
    if isinstance(cell, datetime):
        try:
            return int(cell.timestamp() * 1e9)
        except (TypeError, ValueError, OSError):
            return None
    return None


def _cell(row: Any, name: str) -> Any:
    """``row[name]`` whichever case Yahoo headed the column with."""
    for key in (name, name.capitalize()):
        try:
            if key in row.index:
                return _num(row[key])
        except (AttributeError, TypeError):  # noqa: PERF203 (a plain mapping has no index)
            if key in row:
                return _num(row[key])
    return None


def _underlying_price(root: str) -> float | None:
    try:
        fast = yf.Ticker(root).fast_info
        return _num(fast["lastPrice"] if "lastPrice" in fast else fast.get("last_price"))
    except Exception as exc:  # noqa: BLE001 (a quote is optional context)
        logger.warning("yfinance.options.underlying_price.failed | error=%s", type(exc).__name__)
        return None


def _ohlcv_sync(
    options_ticker: str,
    from_date: str | None,
    to_date: str | None,
    interval: str,
) -> list[dict[str, Any]]:
    step = _INTERVALS.get(interval)
    if step is None and interval != "4hour":
        raise IntervalNotSupported(f"Interval '{interval}' is not supported by yfinance")
    history = yf.Ticker(str(options_ticker).removeprefix("O:")).history(
        interval="1h" if interval == "4hour" else step,
        start=from_date,
        end=_exclusive_end(to_date),
        auto_adjust=False,
    )
    bars = [
        {
            "time": int(stamp.timestamp() * 1000),
            **{k: _cell(row, k) for k in ("open", "high", "low", "close")},
            "volume": _cell(row, "volume") or 0.0,
        }
        for stamp, row in history.iterrows()
        if _cell(row, "close") is not None
    ]
    if interval == "4hour":
        bars = _resample(bars, hours=4)
    return bars


def _exclusive_end(to_date: str | None) -> str | None:
    if not to_date:
        return None
    end = datetime.strptime(str(to_date)[:10], "%Y-%m-%d") + timedelta(days=1)
    return end.strftime("%Y-%m-%d")


def _resample(bars: list[dict[str, Any]], *, hours: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for bar in sorted(bars, key=lambda bar: bar["time"]):
        start = bar["time"] - (bar["time"] % (hours * 3600_000))
        if out and out[-1]["time"] == start:
            last = out[-1]
            highs = [v for v in (last.get("high"), bar.get("high")) if v is not None]
            lows = [v for v in (last.get("low"), bar.get("low")) if v is not None]
            last["high"] = max(highs) if highs else None
            last["low"] = min(lows) if lows else None
            last["close"] = bar["close"]
            last["volume"] = (last.get("volume") or 0.0) + (bar.get("volume") or 0.0)
        else:
            out.append({**bar, "time": start})
    return out


def _ms(cell: Any) -> int | None:
    """A time cell (epoch seconds, ms or a datetime) as Unix ms."""
    if cell is None:
        return None
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        if not math.isfinite(cell):
            return None
        return int(cell * 1000 if cell < 1e11 else cell)
    to_pydatetime = getattr(cell, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            cell = to_pydatetime()
        except (TypeError, ValueError):
            return None
    if isinstance(cell, datetime):
        try:
            return int(cell.timestamp() * 1000)
        except (TypeError, ValueError, OSError):
            return None
    return None


def _movers_sync(direction: str) -> list[MoverRow]:
    from yfinance.screener.screener import screen

    query = _MOVERS.get(str(direction).lower())
    if query is None:
        return []
    try:
        frame = screen(query)
    except Exception as exc:  # noqa: BLE001 (a failed screen is an empty answer)
        logger.warning("yfinance.movers.failed | error=%s", type(exc).__name__)
        return []
    rows: list[MoverRow] = []
    for quote in frame.get("quotes", []) if isinstance(frame, dict) else []:
        price = _num(quote.get("regularMarketPrice"))
        if price is None:
            continue
        traded_at = quote.get("regularMarketTime")
        rows.append(
            MoverRow(
                symbol=str(quote.get("symbol", "")),
                price=price,
                change=_num(quote.get("regularMarketChange")) or 0.0,
                change_percent=_num(quote.get("regularMarketChangePercent")) or 0.0,
                volume=_num(quote.get("regularMarketVolume")) or 0.0,
                traded_value=price * (_num(quote.get("regularMarketVolume")) or 0.0),
                # Unix ms, the unit every reader of a mover's time divides by 1000.
                traded_at=_ms(traded_at),
            )
        )
    return rows


def _info(symbol: str) -> dict[str, Any]:
    info = yf.Ticker(yahoo_symbol(symbol)).info or {}
    return info if isinstance(info, dict) else {}


def _float_sync(symbol: str) -> dict[str, Any]:
    info = _info(symbol)
    free = _num(info.get("floatShares"))
    listed = _num(info.get("sharesOutstanding")) or _num(info.get("impliedSharesOutstanding"))
    if free is None and listed is None:
        return {}
    percent = round(free / listed * 100.0, 2) if free is not None and listed else None
    return {
        "symbol": str(symbol).upper(),
        "free_float": free,
        "listed_shares": listed,
        "free_float_percent": percent,
        "date_filed": None,
    }


def _short_interest_sync(symbol: str, limit: int, sort: str) -> list[dict[str, Any]]:
    info = _info(symbol)
    shares = _num(info.get("sharesShort"))
    if shares is None:
        return []
    when = _num(info.get("dateShortInterest"))
    ratio = _num(info.get("shortRatio"))
    return [
        {
            "ticker": str(symbol).upper(),
            "settlement_date": datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d") if when else None,
            "short_interest": shares,
            "avg_daily_volume": shares / ratio if ratio else None,
            "days_to_cover": ratio,
        }
    ][:limit]


def _short_volume_sync(symbol: str, limit: int, sort: str) -> list[dict[str, Any]]:
    bare = str(symbol).upper().removesuffix(".US")
    if _NON_US.search(bare) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", bare):
        return []
    rows = [
        row
        for day in _finra_days()
        for row in _finra_file(day)
        if row["ticker"] == bare
    ]
    rows.sort(key=lambda row: row["date"], reverse=sort.endswith("desc"))
    return rows[:limit]


def _finra_days() -> list[str]:
    """The latest weekdays, newest first, FINRA publishes a file for."""
    today = datetime.now(timezone(timedelta(hours=-5))).date()
    return [
        (today - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(_FINRA_LOOKBACK_DAYS)
        if (today - timedelta(days=offset)).weekday() < 5
    ]


def _finra_file(day: str) -> list[dict[str, Any]]:
    with _finra_lock:
        hit = _finra_cache.get(day)
        if hit and time.monotonic() - hit[0] < _FINRA_CACHE_TTL:
            return hit[1]
    rows: list[dict[str, Any]] = []
    try:
        response = http_get(FINRA_URL.format(yyyymmdd=day), timeout=8.0)
        response.raise_for_status()
        rows = _parse_finra(response.text)
    except Exception as exc:  # noqa: BLE001 (FINRA is best effort)
        logger.warning("yfinance.short_volume.finra_failed | day=%s error=%s", day, type(exc).__name__)
    with _finra_lock:
        _finra_cache[day] = (time.monotonic(), rows)
    return rows


def _parse_finra(text: str) -> list[dict[str, Any]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [cell.strip() for cell in lines[0].split("|")]
    try:
        at = {name: header.index(name) for name in ("Date", "Symbol", "ShortVolume", "TotalVolume")}
    except ValueError:
        return []
    rows = []
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) <= max(at.values()):
            continue
        try:
            short, total = int(float(cells[at["ShortVolume"]])), int(float(cells[at["TotalVolume"]]))
        except ValueError:
            continue
        raw_day = cells[at["Date"]]
        iso_day = (
            f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:8]}"
            if len(raw_day) == 8 and raw_day.isdigit()
            else raw_day
        )
        rows.append(
            {
                "ticker": cells[at["Symbol"]].upper(),
                "date": iso_day,
                "short_volume": short,
                "total_volume": total,
                "short_volume_ratio": round(short / total * 100.0, 2) if total else None,
            }
        )
    return rows


class YFinanceMarketIntelSource(MarketIntelSource):
    """``MarketIntelSource`` read from Yahoo Finance and FINRA; no key needed."""

    async def get_options_chain(self, underlying: str, user_id: Any = None, **filters: Any) -> dict[str, Any]:
        return await asyncio.to_thread(_chain_sync, underlying, **filters)

    async def get_options_ohlcv(
        self,
        options_ticker: str,
        from_date: str | None = None,
        to_date: str | None = None,
        interval: str = "1hour",
        user_id: Any = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_ohlcv_sync, options_ticker, from_date, to_date, interval)

    async def get_short_interest(
        self, symbol: str, limit: int = 500, sort: str = "settlement_date.asc", user_id: Any = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_short_interest_sync, symbol, int(limit), str(sort))

    async def get_short_volume(
        self, symbol: str, limit: int = 500, sort: str = "date.asc", user_id: Any = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_short_volume_sync, symbol, int(limit), str(sort))

    async def get_float_shares(self, symbol: str, user_id: Any = None) -> dict[str, Any]:
        return await asyncio.to_thread(_float_sync, symbol)

    async def get_options_snapshot(self, tickers: list[str], user_id: Any = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_snapshot_sync, list(tickers))

    async def get_movers(self, direction: str = "gainers", user_id: Any = None) -> list[MoverRow]:
        return await asyncio.to_thread(_movers_sync, direction)

    async def close(self) -> None:
        return None
