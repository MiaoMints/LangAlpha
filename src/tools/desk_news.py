"""The deployment's own news desk, exposed to the brief as a tool.

The dashboard polls a fifteen-source feed every minute, so the brief should
start from rows the desk already holds instead of paying for a second round of
searching. Only compact rows travel: time, source, title and URL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# The desk returns at most a few dozen rows per poll; asking for more than this
# only pads the prompt with older items.
FETCH_LIMIT = 80
MAX_ITEMS = 60
MAX_TITLE_CHARS = 220


def _cutoff(hours: int) -> datetime:
    hours = max(1, min(int(hours), 24))
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _published_at(article: dict[str, Any]) -> datetime | None:
    raw = article.get("published_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _source_name(article: dict[str, Any]) -> str:
    source = article.get("source")
    if isinstance(source, dict):
        name = source.get("name")
        return name if isinstance(name, str) and name else "unknown"
    return str(source) if source else "unknown"


def _row(article: dict[str, Any]) -> str:
    published = _published_at(article)
    stamp = published.astimezone(timezone.utc).strftime("%m-%d %H:%M") if published else "??-?? ??:??"
    title = str(article.get("title") or "").strip().replace("\n", " ")
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1] + "…"
    url = str(article.get("article_url") or "").strip()
    return f"[{stamp}Z] {_source_name(article)} | {title} | {url}"


@tool
async def get_desk_news(hours: int = 1, limit: int = 40) -> str:
    """Headlines the deployment's news desk has already collected.

    The desk merges fifteen Chinese and crypto feeds (Eastmoney, Cailianpress,
    Jin10, WSCN, THS, Sina, PANews, ChainCatcher, Odaily, Wublock, Jinse,
    BlockBeats, Binance, OKX, Cointelegraph, CoinDesk). Every row carries the
    article URL, so a brief built from these rows already cites its sources.

    Call this first, before WebSearch: the desk is warm and cheap, and its rows
    are what the dashboard shows the operator. Use WebSearch afterwards only to
    fill what the desk is missing — a specific company, filing or number.

    Args:
        hours: How far back to look, 1-24 (default 1, the brief window).
        limit: Maximum rows to return, 1-60 (default 40).
    """
    from src.data_client import get_news_data_provider

    cutoff = _cutoff(hours)
    wanted = max(1, min(int(limit), MAX_ITEMS))

    try:
        provider = await get_news_data_provider()
        data = await provider.get_news(limit=FETCH_LIMIT)
    except Exception:
        logger.warning("desk_news.unavailable", exc_info=True)
        return "The news desk could not be reached; fall back to WebSearch."

    articles = data.get("results") or []
    rows: list[str] = []
    sources: set[str] = set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        published = _published_at(article)
        if published is not None and published < cutoff:
            continue
        rows.append(_row(article))
        sources.add(_source_name(article))
        if len(rows) >= wanted:
            break

    if not rows:
        return (
            f"The desk holds nothing in the last {hours}h "
            f"(fetched {len(articles)} older rows). Use WebSearch for this window."
        )

    header = (
        f"{len(rows)} rows from the news desk, last {hours}h, "
        f"{len(sources)} sources: {', '.join(sorted(sources))}. "
        f"Every row already cites its URL."
    )
    return header + "\n" + "\n".join(rows)
