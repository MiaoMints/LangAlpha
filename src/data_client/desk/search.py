"""Eastmoney symbol suggest, the same public search dsh-trading uses."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener


def search_cn(query: str, limit: int = 8) -> list[dict[str, Any]]:
    text = (query or "").strip()
    if not text:
        return []
    url = "https://searchapi.eastmoney.com/api/suggest/get?input=" + quote(text) + "&type=14"
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    items = ((payload.get("QuotationCodeTable") or {}).get("Data")) or []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        code = str(item.get("Code") or "").strip()
        name = str(item.get("Name") or "").strip()
        if not code or not name:
            continue
        if code.isdigit() and len(code) == 6:
            market = "SH" if code[0] in "569" else "SZ"
            symbol = f"{code}.{market}"
            currency = "CNY"
        elif code.isdigit() and len(code) == 5:
            symbol = f"{code}.HK"
            currency = "HKD"
        else:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.append({
            "symbol": symbol,
            "name": name,
            "currency": currency,
            "stockExchange": market if currency == "CNY" else "HK",
            "exchangeShortName": item.get("SecurityTypeName") or "",
        })
        if len(rows) >= limit:
            break
    return rows
