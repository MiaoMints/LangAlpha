"""Where the profile stage meets the agent: the server's row in the tool manifest.

Read when a workspace's tool view is built, never copied onto a row, so a
better description reaches every connected row on its own and no migration
has to rewrite one that went stale.

Precedence: what the user wrote on the row, then the generated description,
then the shipped brokerage text. A brokerage row holds the shipped text from
the day it was connected, so that text, current or retired, counts as
nobody's edit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from src.server.database import mcp_enrichment as db
from src.server.services.brokerages import brokerage_for_url
from src.server.services.mcp_enrichment.assignments import scope_for
from src.server.services.mcp_enrichment.schema import PROFILE_PROMPT_VERSION

logger = logging.getLogger(__name__)


def effective_description(
    row: Mapping[str, Any], profile: Mapping[str, Any] | None
) -> str:
    """The description the agent reads for one catalog row."""
    description = (row.get("description") or "").strip()
    brokerage = brokerage_for_url(row.get("url"))
    if brokerage and description in (
        brokerage.description,
        *brokerage.retired_descriptions,
    ):
        description = ""
    return (
        description
        or ((profile or {}).get("description") or "").strip()
        or (brokerage.description if brokerage else "")
    )


async def profiles_by_server(
    user_id: str, rows: Iterable[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Catalog row name -> its newest profile under the current prompt version."""
    scopes = {row["name"]: scope_for(user_id, row["name"], row.get("url")) for row in rows}
    try:
        found = await db.latest_profiles(
            sorted(set(scopes.values())), prompt_version=PROFILE_PROMPT_VERSION
        )
    except Exception:
        # Prose, not policy: a failed read leaves the row's own text in place.
        logger.warning("[mcp_enrichment] profile read failed", exc_info=True)
        return {}
    return {name: found[s] for name, s in scopes.items() if s in found}
