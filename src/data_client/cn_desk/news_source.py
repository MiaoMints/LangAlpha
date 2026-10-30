"""Chinese desk news: A-share flashes plus crypto flashes.

No API key. The general feed (no tickers) merges Eastmoney kuaixun and
PANews. A ticker query is refused so the provider chain can fall through
to a symbol-specific source.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Eastmoney kuaixun: .../getlist_102_ajaxResult_{pageSize}_{page}_.html
_EAST_PAGE_SIZE = 50
# Cap so a deep scroll cannot walk the flash history forever in one request.
_MAX_EAST_PAGES = 40
_PANEWS = "https://www.panewslab.com/zh/rss/newsflash.xml"


def _east_url(page: int) -> str:
    return (
        "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_"
        f"{_EAST_PAGE_SIZE}_{page}_.html"
    )

_HEADERS = {"User-Agent": "Mozilla/5.0"}


class CnDeskNewsSource:
    async def get_news(
        self,
        tickers: list[str] | None = None,
        limit: int = 20,
        published_after: str | None = None,
        published_before: str | None = None,
        cursor: str | None = None,
        order: str | None = None,
        sort: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        codes = [_cn_code(item) for item in (tickers or [])]
        codes = [code for code in codes if code]
        if tickers and not codes:
            raise RuntimeError("cn-desk serves the general feed only")
        if codes:
            articles = await _announcements(codes, limit)
            return {"results": articles, "count": len(articles), "next_cursor": None}
        articles, has_more = await _load(limit, cursor)
        next_cursor = articles[-1]["id"] if articles and has_more else None
        return {"results": articles, "count": len(articles), "next_cursor": next_cursor}

    async def get_news_article(
        self, article_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        return None

    async def close(self) -> None:
        return None


def _cn_code(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if text.endswith((".SH", ".SZ", ".SS")) and text[:6].isdigit():
        return text[:6]
    if len(text) == 6 and text.isdigit():
        return text
    return ""


async def _announcements(codes: list[str], limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    timeout = httpx.Timeout(12.0)
    async with httpx.AsyncClient(timeout=timeout, headers=_HEADERS, follow_redirects=True, trust_env=False) as client:
        for code in codes[:8]:
            try:
                response = await client.get(
                    "https://np-anotice-stock.eastmoney.com/api/security/ann",
                    params={
                        "page_size": str(max(limit, 10)),
                        "page_index": "1",
                        "ann_type": "A",
                        "client_source": "web",
                        "stock_list": code,
                    },
                )
                response.raise_for_status()
                items = ((response.json() or {}).get("data") or {}).get("list") or []
            except Exception:
                logger.warning("cn_desk.announcement_failed | code=%s", code, exc_info=True)
                continue
            for item in items:
                title = str(item.get("title") or "").strip()
                art = str(item.get("art_code") or "")
                if not title or not art:
                    continue
                rows.append(_article(
                    f"cnd-ann-{art}",
                    title,
                    "",
                    _stamp(str(item.get("display_time") or item.get("notice_date") or "")),
                    f"https://data.eastmoney.com/notices/detail/{code}/{art}.html",
                    "东方财富公告",
                ))
                if len(rows) >= limit:
                    return rows
    return rows


def _stamp(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    try:
        if text[:4].isdigit() and "-" in text[:10]:
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
        else:
            parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.isoformat()
    except (TypeError, ValueError, IndexError):
        return text


def _article(
    article_id: str,
    title: str,
    body: str,
    published_at: str,
    url: str,
    publisher: str,
) -> dict[str, Any]:
    return {
        "id": article_id,
        "title": title.strip(),
        "author": None,
        "description": (body or "").strip() or None,
        "published_at": published_at,
        "article_url": url,
        "image_url": None,
        "source": {
            "name": publisher,
            "logo_url": None,
            "homepage_url": None,
            "favicon_url": None,
        },
        "tickers": [],
        "keywords": [],
        "sentiments": None,
    }


def _slice_page(
    rows: list[dict[str, Any]],
    limit: int,
    cursor: str | None,
    upstream_more: bool,
) -> tuple[list[dict[str, Any]], bool] | None:
    """One page of ``rows`` in stream order, or None if a later page is required.

    ``cursor`` is the id of the last row already shown. The returned flag is
    true when another row exists after this page.
    """
    ordered = rows
    if cursor:
        try:
            start = [row["id"] for row in ordered].index(cursor) + 1
        except ValueError:
            return None if upstream_more else ([], False)
        ordered = ordered[start:]
    if upstream_more and len(ordered) <= limit:
        return None
    return ordered[:limit], len(ordered) > limit


async def _load(
    limit: int, cursor: str | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Interleave the desks and walk that stream until ``limit`` is filled.

    Eastmoney kuaixun is paged (50 per page). PANews and the exchange
    announcements are one snapshot. Rows are appended in the same interleave
    the first page has always used, so a cursor from that page reads the next
    older rows instead of reshuffling the two desks.
    """
    limit = max(int(limit or 1), 1)
    timeout = httpx.Timeout(12.0)
    async with httpx.AsyncClient(
        timeout=timeout, headers=_HEADERS, follow_redirects=True
    ) as client:
        crypto: list[dict[str, Any]] = []
        crypto_seen: set[str] = set()
        _absorb(crypto, crypto_seen, await _panews(client))
        _absorb(crypto, crypto_seen, await _exchange_announcements(client))

        east: list[dict[str, Any]] = []
        east_seen: set[str] = set()
        east_page = 0
        east_more = True

        async def ensure_east(index: int) -> None:
            nonlocal east_page, east_more
            while east_more and len(east) <= index:
                east_page += 1
                if east_page > _MAX_EAST_PAGES:
                    east_more = False
                    return
                batch = await _eastmoney(client, east_page)
                before = len(east)
                _absorb(east, east_seen, batch)
                if (
                    len(batch) < _EAST_PAGE_SIZE
                    or len(east) == before
                    or east_page >= _MAX_EAST_PAGES
                ):
                    east_more = False

        stream: list[dict[str, Any]] = []
        seen: set[str] = set()
        east_index = 0
        crypto_index = 0
        while True:
            await ensure_east(east_index)
            added = False
            if east_index < len(east):
                _absorb(stream, seen, [east[east_index]])
                east_index += 1
                added = True
            if crypto_index < len(crypto):
                _absorb(stream, seen, [crypto[crypto_index]])
                crypto_index += 1
                added = True
            more = east_more or east_index < len(east) or crypto_index < len(crypto)
            if not added:
                sliced = _slice_page(stream, limit, cursor, False)
                return sliced if sliced is not None else ([], False)
            sliced = _slice_page(stream, limit, cursor, more)
            if sliced is not None:
                return sliced


def _absorb(
    pooled: list[dict[str, Any]], seen: set[str], batch: list[dict[str, Any]]
) -> None:
    for row in batch:
        article_id = row.get("id")
        if not row.get("title") or not article_id or article_id in seen:
            continue
        seen.add(article_id)
        pooled.append(row)


async def _eastmoney(client: httpx.AsyncClient, page: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(_east_url(page))
        response.raise_for_status()
        raw = response.text
        payload = json.loads(raw[raw.find("{"):].rstrip().rstrip(";"))
        for item in payload.get("LivesList") or []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            rows.append(
                _article(
                    f"cnd-em-{item.get('id') or title[:24]}",
                    title,
                    str(item.get("digest") or item.get("simdigest") or ""),
                    _stamp(str(item.get("showtime") or "")),
                    str(item.get("url_w") or item.get("url_unique") or ""),
                    "\u4e1c\u65b9\u8d22\u5bcc",
                )
            )
    except Exception:
        logger.warning("cn_desk.eastmoney_failed | page=%s", page, exc_info=True)
    return rows


async def _panews(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(_PANEWS)
        response.raise_for_status()
        for item in re.findall(r"<item>(.*?)</item>", response.text, re.S):
            title = _tag(item, "title")
            if not title:
                continue
            rows.append(
                _article(
                    "cnd-pa-" + (_tag(item, "guid") or title[:24]),
                    title,
                    _tag(item, "description"),
                    _stamp(_tag(item, "pubDate")),
                    _tag(item, "link"),
                    "PANews",
                )
            )
    except Exception:
        logger.warning("cn_desk.panews_failed", exc_info=True)
    return rows


def _tag(item: str, name: str) -> str:
    match = re.search(rf"<{name}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", item, re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


async def _exchange_announcements(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Binance and OKX public announcements. Failures stay off the A-share page."""
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(
            "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
            params={"type": "1", "pageNo": "1", "pageSize": "20"},
            timeout=5.0,
        )
        response.raise_for_status()
        catalogs = ((response.json() or {}).get("data") or {}).get("catalogs") or []
        keep = {48, 161, 51, 157}
        for catalog in catalogs:
            if catalog.get("catalogId") not in keep:
                continue
            for article in catalog.get("articles") or []:
                title = str(article.get("title") or "").strip()
                code = str(article.get("code") or "")
                if not title or not code:
                    continue
                published = article.get("releaseDate")
                stamp = ""
                if isinstance(published, (int, float)):
                    stamp = datetime.fromtimestamp(published / 1000, timezone.utc).isoformat()
                rows.append(_article(
                    f"cnd-bn-{code}",
                    title,
                    "",
                    stamp,
                    f"https://www.binance.com/en/support/announcement/{code}",
                    "Binance",
                ))
    except Exception:
        logger.warning("cn_desk.binance_failed", exc_info=True)
    try:
        response = await client.get("https://www.okx.com/api/v5/support/announcements", timeout=5.0)
        response.raise_for_status()
        details = (((response.json() or {}).get("data") or [{}])[0] or {}).get("details") or []
        for article in details[:20]:
            title = str(article.get("title") or "").strip()
            url = str(article.get("url") or "")
            if not title or not url:
                continue
            published = article.get("pTime")
            stamp = ""
            try:
                stamp = datetime.fromtimestamp(int(published) / 1000, timezone.utc).isoformat()
            except (TypeError, ValueError):
                stamp = ""
            rows.append(_article(f"cnd-okx-{published or title[:24]}", title, "", stamp, url, "OKX"))
    except Exception:
        logger.warning("cn_desk.okx_failed", exc_info=True)
    return rows
