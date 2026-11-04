"""Chinese desk news: A-share flashes plus crypto flashes.

No API key. The general feed (no tickers) merges Eastmoney kuaixun, PANews,
exchange announcements, Jin10, Wallstreetcn, 10jqka, Sina Finance, Cailianpress,
Odaily, ChainCatcher, Wublockchain, Jinse, and the CoinDesk and Cointelegraph
RSS feeds. A ticker query is refused so the provider chain can fall through to a
symbol-specific source.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Jin10 only answers its own page, which passes a client id.
_JIN10 = "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1"
_JIN10_HEADERS = {"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0"}
_WALLSTCN = "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=30"
_THS = "https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website"
_SINA = (
    "https://zhibo.sina.com.cn/api/zhibo/feed"
    "?page=1&page_size=30&zhibo_id=152&tag_id=0&dire=f&dpc=1"
)
_COINTELEGRAPH = "https://cointelegraph.com/rss"
_COINDESK = "https://www.coindesk.com/arc/outboundfeeds/rss/"
_ODAILY = "https://api.odaily.news/api/v1/newsflash"
_WUBLOCK = "https://wublock123.com/rss"
# Cailianpress signs every call: md5 of the sha1 of its sorted query string.
_CLS = "https://www.cls.cn/v1/roll/get_roll_list"
_CLS_QUERY = {"app": "CailianpressWeb", "os": "web", "sv": "8.7.9"}
# ChainCatcher takes a plain JSON POST; ref and language are the only gates.
_CHAINCATCHER = (
    "https://www.api.chaincatcher.com/pc/content/page"
    "?channel=PC&cversion=1.0.0&requestid=PC"
)
_CHAINCATCHER_HEADERS = {
    "ref": "https://www.chaincatcher.com/news",
    "language": "zh-CN",
}
_JINSE = "https://newapi.jinse2.com/noah/v1/flashes/simple"
_JINSE_HEADERS = {"referer": "https://jinse2.com/"}

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


def _epoch(value: Any) -> str:
    """Unix seconds or milliseconds, rendered as the stamp the API expects."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 10_000_000_000:
        number /= 1000.0
    return datetime.fromtimestamp(number, timezone.utc).isoformat()


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _interleave(batches: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Draw one row from each batch in turn, dropping repeats by id."""
    pooled: list[dict[str, Any]] = []
    seen: set[str] = set()
    longest = max((len(batch) for batch in batches), default=0)
    for index in range(longest):
        for batch in batches:
            if index < len(batch):
                _absorb(pooled, seen, [batch[index]])
    return pooled


async def _jin10(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(_JIN10, headers=_JIN10_HEADERS, timeout=8.0)
        response.raise_for_status()
        for item in (response.json() or {}).get("data") or []:
            content = str((item.get("data") or {}).get("content") or "").strip()
            if not content:
                continue
            first, _, rest = content.partition("\n")
            title = re.sub(r"^【(.*?)】.*$", r"\1", first).strip() or first.strip()[:80]
            rows.append(
                _article(
                    "cnd-j10-" + str(item.get("id") or title[:24]),
                    title,
                    rest.strip() or content,
                    _stamp(str(item.get("time") or "")),
                    "https://flash.jin10.com/detail/" + str(item.get("id") or ""),
                    "\u91d1\u5341\u6570\u636e",
                )
            )
    except Exception:
        logger.warning("cn_desk.jin10_failed", exc_info=True)
    return rows


async def _wallstcn(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(_WALLSTCN, timeout=8.0)
        response.raise_for_status()
        items = ((response.json() or {}).get("data") or {}).get("items") or []
        for item in items:
            text = _plain(str(item.get("content_text") or item.get("content") or ""))
            title = str(item.get("title") or "").strip() or text[:80]
            if not title:
                continue
            rows.append(
                _article(
                    "cnd-wscn-" + str(item.get("id") or title[:24]),
                    title,
                    text,
                    _epoch(item.get("display_time")),
                    str(item.get("uri") or ""),
                    "\u534e\u5c14\u8857\u89c1\u95fb",
                )
            )
    except Exception:
        logger.warning("cn_desk.wallstcn_failed", exc_info=True)
    return rows


async def _ths(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(_THS, timeout=8.0)
        response.raise_for_status()
        items = ((response.json() or {}).get("data") or {}).get("list") or []
        for item in items:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            rows.append(
                _article(
                    "cnd-ths-" + str(item.get("id") or title[:24]),
                    title,
                    str(item.get("digest") or ""),
                    _epoch(item.get("rtime") or item.get("ctime")),
                    str(item.get("url") or ""),
                    "\u540c\u82b1\u987a",
                )
            )
    except Exception:
        logger.warning("cn_desk.ths_failed", exc_info=True)
    return rows


async def _sina(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(_SINA, timeout=8.0)
        response.raise_for_status()
        payload = response.json() or {}
        feed = (((payload.get("result") or {}).get("data") or {}).get("feed") or {})
        for item in feed.get("list") or []:
            text = _plain(str(item.get("rich_text") or ""))
            if not text:
                continue
            rows.append(
                _article(
                    "cnd-sina-" + str(item.get("id") or text[:24]),
                    text[:80],
                    text if len(text) > 80 else "",
                    _stamp(str(item.get("create_time") or "")),
                    str(item.get("docurl") or ""),
                    "\u65b0\u6d6a\u8d22\u7ecf",
                )
            )
    except Exception:
        logger.warning("cn_desk.sina_failed", exc_info=True)
    return rows


def _cls_sign(query: dict[str, str]) -> dict[str, str]:
    """Cailianpress wants sign = md5(sha1(k=v&k=v sorted by key))."""
    body = "&".join(f"{key}={query[key]}" for key in sorted(query))
    digest = hashlib.sha1(body.encode()).hexdigest()
    return {**query, "sign": hashlib.md5(digest.encode()).hexdigest()}


async def _cls(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Cailianpress telegraph. The list row already carries the paragraph."""
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(
            _CLS, params=_cls_sign({**_CLS_QUERY, "rn": "30"}), timeout=10.0
        )
        response.raise_for_status()
        payload = (response.json() or {}).get("data") or {}
        for item in payload.get("roll_data") or []:
            title = str(item.get("title") or "").strip()
            brief = str(item.get("brief") or "").strip()
            content = _plain(str(item.get("content") or ""))
            if not title and not brief:
                continue
            rows.append(
                _article(
                    "cnd-cls-" + str(item.get("id") or title[:24]),
                    title or brief[:80],
                    content if len(content) > len(brief) else brief,
                    _epoch(item.get("ctime")),
                    "https://www.cls.cn/detail/" + str(item.get("id") or ""),
                    "\u8d22\u8054\u793e",
                )
            )
    except Exception:
        logger.warning("cn_desk.cls_failed", exc_info=True)
    return rows


async def _chaincatcher(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """ChainCatcher flashes. A plain JSON POST; no signing needed."""
    rows: list[dict[str, Any]] = []
    try:
        response = await client.post(
            _CHAINCATCHER,
            headers=_CHAINCATCHER_HEADERS,
            json={
                "type": 2,
                "newsFlashTypes": [1, 2],
                "pageNumber": 1,
                "pageSize": 30,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        payload = (response.json() or {}).get("data") or {}
        for item in payload.get("items") or []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            rows.append(
                _article(
                    "cnd-cc-" + str(item.get("id") or title[:24]),
                    title,
                    str(item.get("description") or ""),
                    _stamp(
                        str(item.get("createTime") or item.get("releaseTime") or "")
                    ),
                    "https://www.chaincatcher.com/article/"
                    + str(item.get("id") or ""),
                    "ChainCatcher",
                )
            )
    except Exception:
        logger.warning("cn_desk.chaincatcher_failed", exc_info=True)
    return rows


async def _jinse(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Jinse flashes. The public list carries the headline only."""
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(
            _JINSE,
            params={"limit": "20", "flag": "down", "category": "0"},
            headers=_JINSE_HEADERS,
            timeout=10.0,
        )
        response.raise_for_status()
        payload = (response.json() or {}).get("data") or {}
        for item in payload.get("list") or []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            rows.append(
                _article(
                    "cnd-jinse-" + str(item.get("id") or title[:24]),
                    title,
                    "",
                    _epoch(item.get("published_at")),
                    str(item.get("jump_url") or ""),
                    "\u91d1\u8272\u8d22\u7ecf",
                )
            )
    except Exception:
        logger.warning("cn_desk.jinse_failed", exc_info=True)
    return rows


async def _odaily(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Odaily newsflash. The list payload already carries the paragraph text."""
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(
            _ODAILY, params={"page": "1", "size": "30"}, timeout=10.0
        )
        response.raise_for_status()
        payload = (response.json() or {}).get("data") or {}
        for item in payload.get("list") or []:
            title = str(item.get("title") or "").strip()
            link = str(item.get("link") or "").strip()
            if not title or not link:
                continue
            rows.append(
                _article(
                    "cnd-od-" + str(item.get("id") or link[-24:]),
                    title,
                    _plain(str(item.get("content") or "")),
                    _epoch(item.get("publishTimestamp")),
                    link,
                    "Odaily\u661f\u7403\u65e5\u62a5",
                )
            )
    except Exception:
        logger.warning("cn_desk.odaily_failed", exc_info=True)
    return rows


async def _rss(
    client: httpx.AsyncClient, url: str, publisher: str, prefix: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        for item in re.findall(r"<item>(.*?)</item>", response.text, re.S):
            title = _tag(item, "title")
            link = _tag(item, "link")
            if not title or not link:
                continue
            rows.append(
                _article(
                    prefix + ((_tag(item, "guid") or link)[-64:]),
                    title,
                    _tag(item, "description"),
                    _stamp(_tag(item, "pubDate")),
                    link,
                    publisher,
                )
            )
    except Exception:
        logger.warning("cn_desk.rss_failed | %s", url, exc_info=True)
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
        crypto = _interleave(
            [
                await _panews(client),
                await _exchange_announcements(client),
                await _rss(client, _COINTELEGRAPH, "Cointelegraph", "cnd-ct-"),
                await _rss(client, _COINDESK, "CoinDesk", "cnd-cd-"),
                await _odaily(client),
                await _chaincatcher(client),
                await _jinse(client),
                await _blockbeats(),
                await _rss(
                    client,
                    _WUBLOCK,
                    "\u5434\u8bf4\u533a\u5757\u94fe",
                    "cnd-wu-",
                ),
            ]
        )

        zh = _interleave(
            [
                await _jin10(client),
                await _wallstcn(client),
                await _ths(client),
                await _sina(client),
                await _cls(client),
            ]
        )

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
        # Eastmoney is the only paged feed; the snapshots are in memory already
        # and are drawn round-robin so every publisher reaches page one.
        snapshots = [crypto, zh]
        positions = [0] * len(snapshots)
        while True:
            await ensure_east(east_index)
            added = False
            if east_index < len(east):
                _absorb(stream, seen, [east[east_index]])
                east_index += 1
                added = True
            for pool_index, pool in enumerate(snapshots):
                position = positions[pool_index]
                if position < len(pool):
                    _absorb(stream, seen, [pool[position]])
                    positions[pool_index] = position + 1
                    added = True
            more = (
                east_more
                or east_index < len(east)
                or any(positions[i] < len(pool) for i, pool in enumerate(snapshots))
            )
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


async def _blockbeats() -> list[dict[str, Any]]:
    """BlockBeats flashes, relayed through Cloudflare and read from __NUXT__."""
    from src.data_client.cn_desk.blockbeats import fetch_flashes

    rows: list[dict[str, Any]] = []
    for item in await fetch_flashes():
        article_id = str(item.get("article_id") or item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not article_id or not title:
            continue
        rows.append(
            _article(
                "cnd-bb-" + article_id,
                title,
                _plain(str(item.get("content") or "")),
                _epoch(item.get("add_time")),
                "https://www.theblockbeats.info/flash/" + article_id,
                "BlockBeats",
            )
        )
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
