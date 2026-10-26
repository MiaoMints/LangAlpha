"""Put new groupings into force: the allowlists the relay reads, and the
version bump that makes warm sessions re-resolve.

The relay never consults a model or these tables; it reads the
``tool_allowlist`` each grant carries. So a grouping that lands, or a user's
choice, is in force only once every affected grant is rewritten, which is
what this does, through the same per-connection rewrite a consent change uses.
"""

from __future__ import annotations

import logging

from src.server.database import mcp_enrichment as db
from src.server.database.egress_grants import apply_consent_to_active_grants
from src.server.database.mcp_config_version import bump_user_workspaces_mcp_version
from src.server.services.brokerages import brokerage_by_name
from src.server.services.brokerage_capabilities import vendor_for_url

logger = logging.getLogger(__name__)


async def reapply_connections(connections: list[dict]) -> int:
    """Rewrite each connection's active grants and bump each owner once."""
    owners: set[str] = set()
    for c in connections:
        try:
            await apply_consent_to_active_grants(c["connection_id"])
        except Exception:
            logger.warning(
                "[mcp_enrichment] could not reapply policy to connection %s",
                c["connection_id"],
                exc_info=True,
            )
            continue
        owners.add(str(c["user_id"]))
    for user_id in owners:
        await bump_user_workspaces_mcp_version(user_id)
    return len(owners)


async def reapply_brokerage(vendor: str, *, user_id: str | None = None) -> int:
    """Every live connection at this brokerage, or one user's. Returns owners bumped."""
    brokerage = brokerage_by_name(vendor)
    if brokerage is None:
        return 0
    from urllib.parse import urlsplit

    host = urlsplit(brokerage.url).hostname or ""
    connections = [
        c
        for c in await db.connections_at_host(host)
        if vendor_for_url(c["server_url"]) == vendor
        and (user_id is None or str(c["user_id"]) == user_id)
    ]
    return await reapply_connections(connections)
