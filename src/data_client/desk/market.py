"""Public market data used by dsh-trading, in LangAlpha's provider shape.

No API key. China and Hong Kong quotes come from Tencent (qt.gtimg.cn), with
Eastmoney push2 as the fallback. Their bars come from Eastmoney's qfq kline
endpoint, which covers 1m through daily, then Tencent where that interval
exists. Crypto quotes and bars come from Binance's public spot REST.

Symbols this source does not own return nothing, so the rest of the chain
(FMP, Yahoo) still serves US listings. Keyed venues (Tushare, HiThink,
OKX private trade, brokers) are not called.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)
# push2his.eastmoney.com disconnects from this host. Remember that and stop
# paying the failed round trip on every chart load; Tencent still has 5m/30m/daily.
_eastmoney_bars_down = False

_SH = ZoneInfo("Asia/Shanghai")
_HK = ZoneInfo("Asia/Hong_Kong")
_CN_INDEX = {
    "000001.SH", "000016.SH", "000300.SH", "000688.SH", "000905.SH", "000852.SH",
    "399001.SZ", "399006.SZ", "399673.SZ",
}
_BINANCE_INTERVAL = {
    "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
    "1hour": "1h", "4hour": "4h",
}
_EASTMONEY_KLT = {
    "1min": "1", "5min": "5", "15min": "15", "30min": "30", "1hour": "60",
}
_TENCENT_MINUTE = {"5min": "m5", "30min": "m30"}
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _kind(symbol: str) -> str | None:
    text = symbol.strip().upper()
    if re.fullmatch(r"[A-Z0-9]{2,12}-USD", text) or re.fullmatch(r"[A-Z0-9]{2,12}USDT", text):
        return "crypto"
    if re.fullmatch(r"\d{6}\.(SH|SZ|SS)", text):
        return "cn"
    if re.fullmatch(r"\d{1,5}\.HK", text) or text in {"HSI.HK", "^HSI"}:
        return "hk"
    return None


def _binance_symbol(symbol: str) -> str:
    text = symbol.strip().upper()
    if text.endswith("-USD"):
        return text[:-4] + "USDT"
    return text.replace("-", "")


def _cn_code(symbol: str) -> tuple[str, str]:
    code, suffix = symbol.strip().upper().split(".", 1)
    market = "SH" if suffix in {"SH", "SS"} else "SZ"
    return code, market


def _tencent_wire(symbol: str, kind: str) -> str:
    if kind == "hk":
        raw = symbol.strip().upper()
        code = "HSI" if raw in {"HSI.HK", "^HSI"} else raw.split(".")[0].zfill(5)
        return f"hk{code}"
    code, market = _cn_code(symbol)
    return ("sh" if market == "SH" else "sz") + code


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _ms(text: str, zone: ZoneInfo) -> int | None:
    raw = text.strip()
    for fmt, width in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10), ("%Y%m%d%H%M", 12)):
        if len(raw) < width:
            continue
        try:
            parsed = datetime.strptime(raw[:width], fmt).replace(tzinfo=zone)
        except ValueError:
            continue
        return int(parsed.timestamp() * 1000)
    return None


def _in_range(ms: int, start: str | None, end: str | None, zone: ZoneInfo) -> bool:
    day = datetime.fromtimestamp(ms / 1000, zone).date().isoformat()
    if start and day < start[:10]:
        return False
    if end and day > end[:10]:
        return False
    return True


class DeskMarketSource:
    def __init__(self) -> None:
        self._direct = httpx.AsyncClient(timeout=12.0, headers=_HEADERS, trust_env=False, follow_redirects=True)
        self._proxied = httpx.AsyncClient(timeout=12.0, headers=_HEADERS, trust_env=True, follow_redirects=True)

    async def close(self) -> None:
        await self._direct.aclose()
        await self._proxied.aclose()

    async def get_market_status(self, user_id: str | None = None) -> dict[str, Any]:
        # The US session clock belongs to the providers that know it. This
        # source is also registered for the US bucket because BTC-USD routes there.
        raise RuntimeError("desk does not serve the US session clock")

    async def get_snapshots(self, symbols: list[str], asset_type: str = "stocks", user_id: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            kind = _kind(str(symbol))
            if kind is None:
                continue
            try:
                row = await self._snapshot(str(symbol), kind)
            except Exception:
                logger.warning("desk.snapshot.failed | symbol=%s", symbol, exc_info=True)
                continue
            if row is not None:
                rows.append(row)
        return rows

    async def get_daily(self, symbol: str, from_date: str | None = None, to_date: str | None = None, is_index: bool = False, user_id: str | None = None) -> list[dict[str, Any]]:
        kind = _kind(symbol)
        if kind is None:
            return []
        if kind == "crypto":
            return await self._binance_bars(symbol, "1d", from_date, to_date, 500)
        bars = await self._eastmoney_bars(symbol, kind, "101", from_date, to_date, 400)
        if bars:
            return bars
        return await self._tencent_bars(symbol, kind, "day", from_date, to_date, 400)

    async def get_intraday(self, symbol: str, interval: str, from_date: str | None = None, to_date: str | None = None, is_index: bool = False, user_id: str | None = None) -> list[dict[str, Any]]:
        kind = _kind(symbol)
        if kind is None:
            return []
        if kind == "crypto":
            mapped = _BINANCE_INTERVAL.get(interval)
            if mapped is None:
                return []
            return await self._binance_bars(symbol, mapped, from_date, to_date, 500)
        klt = _EASTMONEY_KLT.get(interval)
        if klt is not None:
            bars = await self._eastmoney_bars(symbol, kind, klt, from_date, to_date, 300)
            if bars:
                return bars
        minute = _TENCENT_MINUTE.get(interval)
        if minute is None:
            return []
        return await self._tencent_bars(symbol, kind, minute, from_date, to_date, 300)

    async def _snapshot(self, symbol: str, kind: str) -> dict[str, Any] | None:
        if kind == "crypto":
            return await self._binance_snapshot(symbol)
        row = await self._tencent_snapshot(symbol, kind)
        if row is not None:
            return row
        if kind == "cn":
            return await self._eastmoney_snapshot(symbol)
        return None

    async def _binance_snapshot(self, symbol: str) -> dict[str, Any] | None:
        pair = _binance_symbol(symbol)
        response = await self._proxied.get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": pair})
        if response.status_code != 200:
            return None
        payload = response.json()
        price = _num(payload.get("lastPrice"))
        if price is None:
            return None
        prev = _num(payload.get("prevClosePrice"))
        change = _num(payload.get("priceChange"))
        if change is None and prev is not None:
            change = price - prev
        base = pair[:-4] if pair.endswith("USDT") else pair
        volume = _num(payload.get("quoteVolume"))
        return {
            "symbol": symbol,
            "name": base,
            "currency": "USD",
            "asset_class": "crypto",
            "price": price,
            "change": change,
            "change_percent": _num(payload.get("priceChangePercent")),
            "previous_close": prev,
            "open": _num(payload.get("openPrice")),
            "high": _num(payload.get("highPrice")),
            "low": _num(payload.get("lowPrice")),
            "volume": int(volume) if volume is not None else None,
            "as_of": int(payload.get("closeTime") or 0) or None,
        }

    async def _tencent_snapshot(self, symbol: str, kind: str) -> dict[str, Any] | None:
        wire = _tencent_wire(symbol, kind)
        quote_wire = "r_" + wire if kind == "hk" else wire
        response = await self._direct.get(f"https://qt.gtimg.cn/q={quote_wire}")
        if response.status_code != 200:
            return None
        text = response.content.decode("gbk", errors="replace")
        match = re.search(r'="([^"]*)"', text)
        if not match:
            return None
        fields = match.group(1).split("~")
        if len(fields) < 35:
            return None
        price = _num(fields[3])
        if price is None or price <= 0:
            return None
        volume = _num(fields[6])
        if volume is not None and kind == "cn":
            volume *= 100
        name = (fields[1] or "").strip() or None
        canonical = symbol.upper().lstrip("^")
        asset = "index" if canonical in _CN_INDEX or canonical in {"HSI.HK", "HSI"} else "equity"
        return {
            "symbol": symbol,
            "name": name,
            "name_local": name,
            "currency": "HKD" if kind == "hk" else "CNY",
            "asset_class": asset,
            "price": price,
            "change": _num(fields[31]),
            "change_percent": _num(fields[32]),
            "previous_close": _num(fields[4]),
            "open": _num(fields[5]),
            "high": _num(fields[33]),
            "low": _num(fields[34]),
            "volume": int(volume) if volume is not None else None,
        }

    async def _eastmoney_snapshot(self, symbol: str) -> dict[str, Any] | None:
        code, market = _cn_code(symbol)
        secid = f"{'1' if market == 'SH' else '0'}.{code}"
        response = await self._direct.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": secid, "fields": "f43,f44,f45,f46,f47,f58,f60,f169,f170"},
            headers={**_HEADERS, "Referer": "https://quote.eastmoney.com/"},
        )
        if response.status_code != 200:
            return None
        data = (response.json() or {}).get("data") or {}
        price = _num(data.get("f43"))
        if price is None:
            return None
        # Eastmoney quote fields are hundredths (f43 133500 = 1335.00).
        price = price / 100
        prev = _num(data.get("f60"))
        if prev is not None:
            prev = prev / 100
        name = str(data.get("f58") or "").strip() or None
        canonical = f"{code}.{market}"
        return {
            "symbol": symbol,
            "name": name,
            "name_local": name,
            "currency": "CNY",
            "asset_class": "index" if canonical in _CN_INDEX else "equity",
            "price": price,
            "change": (None if _num(data.get("f169")) is None else _num(data.get("f169")) / 100),
            "change_percent": (None if _num(data.get("f170")) is None else _num(data.get("f170")) / 100),
            "previous_close": prev,
            "open": (None if not _num(data.get("f46")) else _num(data.get("f46")) / 100),
            "high": (None if not _num(data.get("f44")) else _num(data.get("f44")) / 100),
            "low": (None if not _num(data.get("f45")) else _num(data.get("f45")) / 100),
            "volume": int(_num(data.get("f47")) or 0) or None,
        }

    async def _binance_bars(self, symbol: str, interval: str, start: str | None, end: str | None, limit: int) -> list[dict[str, Any]]:
        try:
            response = await self._proxied.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": _binance_symbol(symbol), "interval": interval, "limit": limit},
            )
            if response.status_code != 200:
                return []
            rows = response.json()
        except Exception:
            logger.warning("desk.binance.bars.failed | symbol=%s", symbol, exc_info=True)
            return []
        bars: list[dict[str, Any]] = []
        if not isinstance(rows, list):
            return []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            ms = int(row[0])
            if not _in_range(ms, start, end, _SH):
                continue
            bars.append({
                "time": ms,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        return bars

    async def _eastmoney_bars(self, symbol: str, kind: str, klt: str, start: str | None, end: str | None, limit: int) -> list[dict[str, Any]]:
        global _eastmoney_bars_down
        if _eastmoney_bars_down or kind != "cn":
            return []
        code, market = _cn_code(symbol)
        secid = f"{'1' if market == 'SH' else '0'}.{code}"
        try:
            response = await self._direct.get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={
                    "secid": secid, "klt": klt, "fqt": "1", "lmt": str(limit),
                    "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56",
                },
                headers={**_HEADERS, "Referer": "https://quote.eastmoney.com/"},
            )
            if response.status_code != 200:
                return []
            lines = ((response.json() or {}).get("data") or {}).get("klines") or []
        except Exception:
            _eastmoney_bars_down = True
            logger.warning("desk.eastmoney.bars.down | symbol=%s", symbol)
            return []
        bars: list[dict[str, Any]] = []
        for line in lines:
            parts = str(line).split(",")
            if len(parts) < 6:
                continue
            ms = _ms(parts[0], _SH)
            if ms is None or not _in_range(ms, start, end, _SH):
                continue
            bars.append({
                "time": ms,
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        return bars

    async def _tencent_bars(self, symbol: str, kind: str, tf: str, start: str | None, end: str | None, limit: int) -> list[dict[str, Any]]:
        wire = _tencent_wire(symbol, kind)
        minute = tf in {"m5", "m30"}
        if minute and kind == "hk":
            return []
        if minute:
            url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={wire},{tf},,{limit}"
            key = tf
        else:
            path = "hkfqkline" if kind == "hk" else "fqkline"
            url = f"https://web.ifzq.gtimg.cn/appstock/app/{path}/get?param={wire},{tf},,,{limit},qfq"
            key = "qfq" + tf
        try:
            response = await self._direct.get(url)
            if response.status_code != 200:
                return []
            payload = response.json()
        except Exception:
            logger.warning("desk.tencent.bars.failed | symbol=%s", symbol, exc_info=True)
            return []
        market = (payload.get("data") or {}).get(wire) or {}
        raw = market.get(key) or market.get(tf) or []
        zone = _HK if kind == "hk" else _SH
        bars: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, list) or len(row) < 6:
                continue
            ms = _ms(str(row[0]), zone)
            if ms is None or not _in_range(ms, start, end, zone):
                continue
            bars.append({
                "time": ms,
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
        return bars
