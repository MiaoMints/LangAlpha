"""DeepSeek web search provider.

DeepSeek publishes a standalone search endpoint (``POST /v1/tools/search``)
billed per successful call, so it belongs at the end of a fallback chain:
free-tier providers are tried first and DeepSeek only answers when the
earlier links fail or run out of quota.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import httpx
from langchain_core.tools import tool

from src.tools.web.providers._shared import (
    lazy,
    request_json,
    result_card,
    search_failed,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
MAX_QUERY_CHARS = 128
MAX_COUNT = 50
SNIPPET_CHARS = 600

_TIME_RANGE_DAYS: Dict[str, int] = {
    "h": 0,
    "d": 1,
    "day": 1,
    "w": 7,
    "week": 7,
    "m": 30,
    "month": 30,
    "y": 365,
    "year": 365,
}


def _apply_time_range(query: str, time_range: Optional[str]) -> str:
    """Express recency as DeepSeek's ``after:`` search syntax."""
    if not time_range:
        return query
    days = _TIME_RANGE_DAYS.get(str(time_range).lower())
    if days is None:
        return query
    cutoff = date.today() - timedelta(days=max(days, 1))
    return f"{query} after:{cutoff.isoformat()}"


def _truncate(text: Any, limit: int = SNIPPET_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


class DeepSeekSearchAPI:
    """Thin sync-style wrapper around DeepSeek's search endpoint."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self._api_key:
            raise ValueError("DEEPSEEK_API_KEY not provided or found in environment")
        self._base_url = (
            base_url
            or os.environ.get("DEEPSEEK_SEARCH_BASE_URL", "")
            or DEFAULT_BASE_URL
        ).rstrip("/")

    async def fetch(
        self,
        query: str,
        count: int = 10,
        need_content: bool = False,
    ) -> Dict[str, Any]:
        """Call the search endpoint and return the raw JSON payload."""
        payload = {
            "query": query[:MAX_QUERY_CHARS],
            "count": max(1, min(int(count), MAX_COUNT)),
            "need_content": bool(need_content),
        }
        return await request_json(
            "POST",
            f"{self._base_url}{SEARCH_PATH}",
            provider="DeepSeek",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json_body=payload,
            timeout=30.0,
        )

    def _format(
        self,
        raw: Dict[str, Any],
        query: str,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        rows = raw.get("results") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            rows = []

        detailed_results: List[Dict[str, Any]] = []
        cards: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "")
            title = str(row.get("title") or "")
            snippet = _truncate(row.get("snippet") or row.get("summary") or row.get("content"))
            if not url:
                continue
            detailed_results.append(
                {
                    "title": title,
                    "url": url,
                    "content": _truncate(row.get("summary") or row.get("content") or snippet),
                    "score": row.get("rerank_score"),
                    "site_name": row.get("site_name"),
                    "date": row.get("date"),
                }
            )
            cards.append(result_card(title=title, url=url, snippet=snippet))

        metadata: Dict[str, Any] = {
            "type": "web_search",
            "query": query,
            "search_type": "general",
            "search_engine": "deepseek",
            "total_results": len(detailed_results),
            "results": cards,
        }
        return detailed_results, metadata


def build_web_search_tool(
    max_results: int = 10,
    default_time_range: Optional[str] = None,
    verbose: bool = True,
):
    """Build a per-request DeepSeek web_search tool."""
    _get_api_wrapper = lazy(DeepSeekSearchAPI)

    @tool(response_format="content_and_artifact")
    async def web_search(
        query: str,
        time_range: Optional[Literal["day", "week", "month", "year", "d", "w", "m", "y"]] = None,
    ) -> tuple[Union[list[dict[str, Any]], str], dict[str, Any]]:
        """Search the web with DeepSeek's built-in search service.

        Use when you need:
        - Current events, news, or facts beyond the knowledge cutoff
        - Verification of a specific claim or figure
        - Chinese-language sources (DeepSeek indexes zh content well)

        Args:
            query: Search query. Supports ``site:``, ``-term``, ``"exact phrase"``,
                ``lang:zh,en`` and ``after:YYYY-MM-DD`` operators.
            time_range: Optional recency filter, translated to ``after:``.

        Returns:
            Ranked results as titles, snippets and URLs — not full page text.
        """
        effective_query = _apply_time_range(query, time_range or default_time_range)
        try:
            api = _get_api_wrapper()
            logger.debug(
                "Executing DeepSeek search: query=%r, count=%s", effective_query, max_results
            )
            raw = await api.fetch(effective_query, count=max_results)
            detailed_results, metadata = api._format(raw, query)
            logger.debug("DeepSeek search completed: %s results", len(detailed_results))
            return detailed_results, metadata
        except httpx.HTTPError as e:
            logger.error("DeepSeek search failed: %s", e, exc_info=True)
            return search_failed(e, query)

    return web_search
