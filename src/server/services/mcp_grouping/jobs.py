"""Discovery's hook into the grouping queue.

Called wherever a shipped brokerage's tool list is cached. It enqueues only when
a tool the code does not decide has no current classification, so an unchanged
rediscovery (the common case) costs one read and writes nothing. The sync that
called it never waits on a model: the worker picks the job up on its own.
"""

from __future__ import annotations

import logging
from typing import Any

from src.config.settings import get_mcp_grouping_config
from src.server.database import mcp_grouping as db
from src.server.database.pool import get_db_connection
from src.server.services.brokerage_capabilities import is_curated, vendor_for_url
from src.server.services.mcp_grouping.assignments import brokerage_scope, platform_model
from src.server.services.mcp_grouping.digest import tool_digest
from src.server.services.mcp_grouping.schema import GROUPING_PROMPT_VERSION

logger = logging.getLogger(__name__)


async def needs_run(brokerage: str, tools: list[dict[str, Any]], *, conn=None) -> bool:
    """Whether a tool the code does not decide lacks a classification under the
    platform model, the only one the gate reads."""
    model = platform_model()
    if model is None:
        return False
    classified = await db.classifications_for(
        [brokerage_scope(brokerage)], prompt_version=GROUPING_PROMPT_VERSION, model=model,
        conn=conn,
    )
    ungrouped = {
        (t.get("name") or "", tool_digest(t))
        for t in tools
        if not is_curated(brokerage, t.get("name") or "")
    }
    return bool(ungrouped - {(name, digest) for _, name, digest in classified})


async def enqueue_if_new(
    *, server_name: str, url: str | None, tools: list[dict[str, Any]], conn=None
) -> bool:
    """Queue a shipped brokerage's tool list if it holds an unclassified tool.
    Never raises: a failure here must not fail the discovery write it rides on."""
    vendor = vendor_for_url(url)
    if vendor is None or not tools or not get_mcp_grouping_config().enabled:
        return False
    scope = brokerage_scope(vendor)
    try:
        # A savepoint inside a caller's transaction, so a failure here rolls
        # back this alone and the discovery write still commits.
        async with get_db_connection(conn) as db_conn, db_conn.transaction():
            if not await needs_run(vendor, tools, conn=db_conn):
                return False
            await db.enqueue_job(
                scope_key=scope, server_name=server_name, tools=tools, conn=db_conn
            )
    except Exception:
        logger.warning(
            "[mcp_grouping] could not queue %s for %s", server_name, scope, exc_info=True
        )
        return False
    logger.info("[mcp_grouping] queued %s (%d tools)", scope, len(tools))
    return True
