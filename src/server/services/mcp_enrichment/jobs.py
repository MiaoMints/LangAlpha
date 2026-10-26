"""Discovery's hook into the enrichment queue.

Called wherever a server's tool list is cached. It enqueues only when the list
holds content no current row covers, so an unchanged rediscovery (the common
case) costs a few reads and writes nothing. The sync that called it never
waits on a model: the worker picks the job up on its own.
"""

from __future__ import annotations

import logging
from typing import Any

from src.config.settings import get_mcp_enrichment_config
from src.server.database import mcp_enrichment as db
from src.server.database.pool import get_db_connection
from src.server.services.brokerage_capabilities import is_curated, vendor_for_url
from src.server.services.mcp_enrichment.assignments import (
    brokerage_scope,
    platform_model,
    user_scope,
)
from src.server.services.mcp_enrichment.digest import (
    described_tools,
    server_digest,
    tool_digest,
)
from src.server.services.mcp_enrichment.schema import (
    GROUPING_PROMPT_VERSION,
    PROFILE_PROMPT_VERSION,
)

logger = logging.getLogger(__name__)


async def needs_run(scope_key: str, brokerage: str | None, tools: list[dict[str, Any]], *, conn=None) -> bool:
    """Whether the server lacks a current description, or a brokerage tool the
    code does not decide lacks a current classification.

    A shared scope is current only under the platform model, the one the gate
    reads. A user's scope is current under any model, since the user's model
    is only known by resolving their settings, which the worker does.
    """
    model = platform_model() if brokerage else None
    if brokerage and model is None:
        return False
    if brokerage:
        classified = await db.classifications_for(
            [scope_key], prompt_version=GROUPING_PROMPT_VERSION, model=model, conn=conn
        )
        ungrouped = {
            (t.get("name") or "", tool_digest(t))
            for t in tools
            if not is_curated(brokerage, t.get("name") or "")
        }
        if ungrouped - {(name, digest) for _, name, digest in classified}:
            return True
    return not await db.has_profile(
        scope_key, server_digest(described_tools(brokerage, tools)), model=model,
        prompt_version=PROFILE_PROMPT_VERSION, conn=conn,
    )


async def enqueue_if_new(
    *,
    user_id: str,
    server_name: str,
    url: str | None,
    tools: list[dict[str, Any]],
    conn=None,
) -> bool:
    """Queue this tool list if anything in it is new. Never raises: a failure
    here must not fail the discovery write it rides on."""
    config = get_mcp_enrichment_config()
    if not config.enabled or not tools:
        return False
    vendor = vendor_for_url(url)
    if vendor is None and not config.user_servers:
        return False
    scope = brokerage_scope(vendor) if vendor else user_scope(user_id, server_name)
    try:
        # A savepoint inside a caller's transaction, so a failure here rolls
        # back this alone and the discovery write still commits.
        async with get_db_connection(conn) as db_conn, db_conn.transaction():
            if not await needs_run(scope, vendor, tools, conn=db_conn):
                return False
            await db.enqueue_job(
                scope_key=scope,
                user_id=None if vendor else user_id,
                server_name=server_name,
                tools=tools,
                conn=db_conn,
            )
    except Exception:
        logger.warning(
            "[mcp_enrichment] could not queue %s for %s", server_name, scope, exc_info=True
        )
        return False
    logger.info("[mcp_enrichment] queued %s (%d tools)", scope, len(tools))
    return True
