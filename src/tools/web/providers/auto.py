"""Composite search provider: try several providers in order.

Every provider builder in this package resolves its own key lazily and reports
failure by returning a "Search failed" *string* instead of raising, so this
module only needs to advance to the next provider whenever the previous one
produced a string (or raised). That keeps the free-tier providers first and
DeepSeek as the paid last resort.
"""

from __future__ import annotations

import inspect
import logging
import os
from importlib import import_module
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from langchain_core.tools import tool

from src.tools.web.providers._shared import search_failed

logger = logging.getLogger(__name__)

DEFAULT_CHAIN: Tuple[str, ...] = ("tavily", "serper", "deepseek")
CHAIN_ENV = "SEARCH_FALLBACK_CHAIN"
RESERVED_NAMES = frozenset({"auto"})


def provider_chain() -> List[str]:
    """Resolve the fallback order from ``SEARCH_FALLBACK_CHAIN`` or the default."""
    raw = os.environ.get(CHAIN_ENV, "")
    override = [name.strip() for name in raw.split(",") if name.strip()]
    chain = override or [name for name in DEFAULT_CHAIN if name not in RESERVED_NAMES]
    return [name for name in chain if name not in RESERVED_NAMES]


def _filter_kwargs(target: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    try:
        params = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in params}


async def _run_provider(
    name: str,
    *,
    query: str,
    time_range: Optional[str],
    max_results: int,
    default_time_range: Optional[str],
    verbose: bool,
) -> Tuple[Any, Any]:
    module = import_module(f"src.tools.web.providers.{name}")
    builder = getattr(module, "build_web_search_tool", None)
    if builder is None:
        raise RuntimeError(f"provider '{name}' has no build_web_search_tool()")

    inner = builder(
        **_filter_kwargs(
            builder,
            {
                "max_results": max_results,
                "default_time_range": default_time_range,
                "verbose": verbose,
            },
        )
    )
    runner = getattr(inner, "coroutine", None) or getattr(inner, "func", None)
    if runner is None or not inspect.iscoroutinefunction(runner):
        raise RuntimeError(f"provider '{name}' exposes no async tool body")
    result = await runner(
        **_filter_kwargs(runner, {"query": query, "time_range": time_range})
    )
    if not isinstance(result, tuple) or len(result) != 2:
        raise RuntimeError(f"provider '{name}' returned an unexpected payload")
    return result


def build_web_search_tool(
    max_results: int = 10,
    default_time_range: Optional[str] = None,
    verbose: bool = True,
):
    """Build a per-request web_search tool that walks the provider chain."""

    @tool(response_format="content_and_artifact")
    async def web_search(
        query: str,
        time_range: Optional[Literal["day", "week", "month", "year", "d", "w", "m", "y"]] = None,
    ) -> tuple[Union[list[dict[str, Any]], str], dict[str, Any]]:
        """Search the web for current information, news, and facts.

        Several search backends are tried in order, so a missing key or an
        exhausted free quota does not break the search.

        Args:
            query: Search query to execute.
            time_range: Optional recency filter - 'day', 'week', 'month' or 'year'.

        Returns:
            Ranked results as titles, snippets and URLs — not full page text.
        """
        chain = provider_chain()
        last_failure: Optional[Tuple[Any, Any]] = None
        empty_result: Optional[Tuple[Any, Any]] = None

        for name in chain:
            try:
                content, artifact = await _run_provider(
                    name,
                    query=query,
                    time_range=time_range,
                    max_results=max_results,
                    default_time_range=default_time_range,
                    verbose=verbose,
                )
            except Exception as e:  # missing key, network error, bad payload
                logger.warning("search provider '%s' unavailable: %s", name, e)
                last_failure = search_failed(e, query)
                continue

            if isinstance(content, str):
                logger.warning(
                    "search provider '%s' returned no results: %s", name, content[:200]
                )
                last_failure = (content, artifact)
                continue

            if not content:
                logger.warning("search provider '%s' returned an empty result set", name)
                empty_result = (content, artifact)
                continue

            logger.info("search answered by '%s' (%d results)", name, len(content))
            if isinstance(artifact, dict):
                artifact = {**artifact, "search_provider": name}
            return content, artifact

        if empty_result is not None:
            content, artifact = empty_result
            if isinstance(artifact, dict):
                artifact = {**artifact, "search_provider": chain[-1]}
            return content, artifact
        if last_failure is not None:
            return last_failure
        return search_failed(
            RuntimeError(f"no search providers configured ({', '.join(chain)})"), query
        )

    return web_search
