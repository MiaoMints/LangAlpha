"""Content identities for cached rows.

A tool's digest covers everything a vendor can change about it that could
change what it does or what a model would say about it, and nothing else, so a
vendor adding one tool reclassifies that tool alone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from src.server.services.brokerage_capabilities import is_uncurated


def _sha(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def tool_digest(tool: dict[str, Any]) -> str:
    """Hash of a cached tool's name, description, input schema and annotations."""
    return _sha(
        {
            "name": tool.get("name") or "",
            "description": tool.get("description") or "",
            "inputSchema": tool.get("input_schema") or {},
            "annotations": tool.get("annotations") or {},
        }
    )


def described_tools(
    brokerage: str | None, tools: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The tools a server's description covers.

    At a shipped brokerage, every tool but those ``UNCURATED`` refuses at every
    grant, so the agent is never told the server does what no grant lets it
    call. The set is the same for every user, so the shared row stays shared,
    and a release that changes it changes the digest.
    """
    return [t for t in tools if not is_uncurated(brokerage, t.get("name") or "")]


def server_digest(tools: Iterable[dict[str, Any]]) -> str:
    """Hash of a server's whole tool surface, order-independent."""
    return _sha(sorted(tool_digest(t) for t in tools))
