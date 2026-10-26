"""SQL for model-written MCP server descriptions, brokerage groupings, and their job queue.

Every read takes the model and prompt version it wants, so a row written under
another one is invisible rather than wrong; nothing here deletes a result row.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection
from src.server.database.sql_fences import advisory_key

# A job no worker finished within this long is claimable again: its worker died
# or lost its database session mid-run.
JOB_LEASE_S = 600
MAX_JOB_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


async def enqueue_job(
    *,
    scope_key: str,
    user_id: str | None,
    server_name: str,
    tools: list[dict[str, Any]],
    conn=None,
) -> None:
    """Queue one scope's tool surface. A pending row for the scope takes the
    newer list and runs at once; a running one is left alone and this queues
    behind it."""
    async with get_db_connection(conn) as db:
        await db.execute(
            """
            INSERT INTO mcp_enrichment_jobs (scope_key, user_id, server_name, tools)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (scope_key) WHERE status = 'pending' DO UPDATE
            SET tools = EXCLUDED.tools, user_id = EXCLUDED.user_id,
                server_name = EXCLUDED.server_name, attempts = 0,
                run_after = NOW(), error = '', updated_at = NOW()
            """,
            (scope_key, user_id, server_name, Json(tools)),
        )


async def claim_job(*, lease_s: int = JOB_LEASE_S) -> dict[str, Any] | None:
    """Take one due job, or None.

    Each candidate is claimed under its scope's advisory lock, inside which no
    other live claim on the scope may exist, so two workers never run one scope
    at once even when a stale claim and a newer pending row coexist.
    """
    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT job_id, scope_key FROM mcp_enrichment_jobs
                WHERE (status = 'pending' AND run_after <= NOW())
                   OR (status = 'running' AND lease_until < NOW())
                ORDER BY run_after
                LIMIT 10
                FOR UPDATE SKIP LOCKED
                """
            )
            for candidate in await cur.fetchall():
                await cur.execute(
                    "SELECT pg_try_advisory_xact_lock(%s) AS got",
                    (advisory_key("MCP_ENRICH", candidate["scope_key"]),),
                )
                if not (await cur.fetchone())["got"]:
                    continue
                await cur.execute(
                    """
                    UPDATE mcp_enrichment_jobs j
                    SET status = 'running', attempts = attempts + 1,
                        lease_until = NOW() + make_interval(secs => %s),
                        updated_at = NOW()
                    WHERE j.job_id = %s AND NOT EXISTS (
                        SELECT 1 FROM mcp_enrichment_jobs o
                        WHERE o.scope_key = j.scope_key AND o.job_id <> j.job_id
                          AND o.status = 'running' AND o.lease_until >= NOW()
                    )
                    RETURNING *
                    """,
                    (lease_s, candidate["job_id"]),
                )
                if row := await cur.fetchone():
                    return row
    return None


async def finish_job(job_id: int) -> None:
    async with get_db_connection() as conn:
        await conn.execute("DELETE FROM mcp_enrichment_jobs WHERE job_id = %s", (job_id,))


async def fail_job(job_id: int, error: str, *, retry_after_s: int) -> None:
    """Back a job off, or give up on it after ``MAX_JOB_ATTEMPTS``.

    A newer pending row for the same scope supersedes this one outright: it
    carries the newer tool list, and the unique pending index has room for one.
    """
    async with get_db_connection() as conn, conn.transaction():
        await conn.execute(
            """
            DELETE FROM mcp_enrichment_jobs j
            WHERE j.job_id = %s AND EXISTS (
                SELECT 1 FROM mcp_enrichment_jobs o
                WHERE o.scope_key = j.scope_key AND o.status = 'pending'
                  AND o.job_id <> j.job_id
            )
            """,
            (job_id,),
        )
        await conn.execute(
            """
            UPDATE mcp_enrichment_jobs
            SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                run_after = NOW() + make_interval(secs => %s),
                lease_until = NULL, error = %s, updated_at = NOW()
            WHERE job_id = %s
            """,
            (MAX_JOB_ATTEMPTS, retry_after_s, error[:2000], job_id),
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


async def insert_profile(
    *,
    scope_key: str,
    server_digest: str,
    model: str,
    prompt_version: str,
    description: str,
) -> None:
    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO mcp_server_profiles
                (scope_key, server_digest, model, prompt_version, description)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (scope_key, server_digest, model, prompt_version, description),
        )


async def insert_classifications(rows: Iterable[dict[str, Any]]) -> None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """
            INSERT INTO mcp_tool_classifications
                (scope_key, tool_name, tool_digest, model, prompt_version,
                 group_key, effect, model_effect, rule, confidence, reason)
            VALUES (%(scope_key)s, %(tool_name)s, %(tool_digest)s, %(model)s,
                    %(prompt_version)s, %(group_key)s, %(effect)s,
                    %(model_effect)s, %(rule)s, %(confidence)s, %(reason)s)
            ON CONFLICT DO NOTHING
            """,
            list(rows),
        )


async def latest_profiles(
    scope_keys: Sequence[str], *, prompt_version: str, conn=None
) -> dict[str, dict[str, Any]]:
    """The newest profile per scope under this prompt version, any model or
    surface: a profile is prose about the server, still right when one tool moves."""
    if not scope_keys:
        return {}
    async with get_db_connection(conn) as db, db.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT DISTINCT ON (scope_key) scope_key, description
            FROM mcp_server_profiles
            WHERE scope_key = ANY(%s) AND prompt_version = %s
            ORDER BY scope_key, created_at DESC
            """,
            (list(scope_keys), prompt_version),
        )
        return {r["scope_key"]: r for r in await cur.fetchall()}


async def classifications_for(
    scope_keys: Sequence[str], *, prompt_version: str, model: str, conn=None
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """(scope, tool, digest) -> the classification under exactly this model and
    version, the only ones the gate may read."""
    if not scope_keys:
        return {}
    async with get_db_connection(conn) as db, db.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT * FROM mcp_tool_classifications
            WHERE scope_key = ANY(%s) AND prompt_version = %s AND model = %s
            """,
            (list(scope_keys), prompt_version, model),
        )
        return {
            (r["scope_key"], r["tool_name"], r["tool_digest"]): r
            for r in await cur.fetchall()
        }


async def has_profile(
    scope_key: str,
    server_digest: str,
    *,
    model: str | None,
    prompt_version: str,
    conn=None,
) -> bool:
    async with get_db_connection(conn) as db, db.cursor() as cur:
        await cur.execute(
            """
            SELECT 1 FROM mcp_server_profiles
            WHERE scope_key = %s AND server_digest = %s AND prompt_version = %s
              AND (%s::text IS NULL OR model = %s)
            LIMIT 1
            """,
            (scope_key, server_digest, prompt_version, model, model),
        )
        return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# What the gate reads beside the results
# ---------------------------------------------------------------------------


async def user_server_snapshots(user_id: str, *, conn=None) -> list[dict[str, Any]]:
    """Each enabled catalog row's address with its newest discovered tools."""
    async with get_db_connection(conn) as db, db.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT s.name, s.url, t.tools
            FROM user_mcp_servers s
            JOIN LATERAL (
                SELECT tools FROM user_mcp_tool_schemas t
                WHERE t.user_id = s.user_id AND t.server_name = s.name
                  AND t.status = 'ok'
                ORDER BY t.discovered_at DESC
                LIMIT 1
            ) t ON TRUE
            WHERE s.user_id = %s
            """,
            (user_id,),
        )
        return list(await cur.fetchall())


async def get_choices(
    user_id: str, brokerages: Sequence[str], *, conn=None
) -> dict[str, dict[str, str | None]]:
    """brokerage -> tool name -> the user's group, None meaning refused."""
    if not brokerages:
        return {}
    async with get_db_connection(conn) as db, db.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT brokerage, tool_name, group_key FROM mcp_tool_choices
            WHERE user_id = %s AND brokerage = ANY(%s)
            """,
            (user_id, list(brokerages)),
        )
        out: dict[str, dict[str, str | None]] = {}
        for r in await cur.fetchall():
            out.setdefault(r["brokerage"], {})[r["tool_name"]] = r["group_key"]
        return out


async def set_choice(
    user_id: str, brokerage: str, tool_name: str, group_key: str | None, *, conn=None
) -> None:
    async with get_db_connection(conn) as db:
        await db.execute(
            """
            INSERT INTO mcp_tool_choices (user_id, brokerage, tool_name, group_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, brokerage, tool_name) DO UPDATE
            SET group_key = EXCLUDED.group_key, updated_at = NOW()
            """,
            (user_id, brokerage, tool_name, group_key),
        )


async def clear_choice(user_id: str, brokerage: str, tool_name: str, *, conn=None) -> None:
    async with get_db_connection(conn) as db:
        await db.execute(
            "DELETE FROM mcp_tool_choices"
            " WHERE user_id = %s AND brokerage = %s AND tool_name = %s",
            (user_id, brokerage, tool_name),
        )


async def connections_at_host(host: str) -> list[dict[str, Any]]:
    """Every live connection whose consented address is on this host."""
    async with get_db_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT connection_id::text AS connection_id, user_id, server_url
            FROM user_mcp_oauth_connections
            WHERE status <> 'revoked' AND server_url ILIKE %s
            """,
            (f"%://{host}%",),
        )
        return list(await cur.fetchall())


# ---------------------------------------------------------------------------
# Curation queue
# ---------------------------------------------------------------------------


async def open_curation_items(rows: Iterable[dict[str, Any]]) -> None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            """
            INSERT INTO mcp_curation_items
                (brokerage, tool_name, tool_digest, effect, group_key, reason)
            VALUES (%(brokerage)s, %(tool_name)s, %(tool_digest)s, %(effect)s,
                    %(group_key)s, %(reason)s)
            ON CONFLICT (brokerage, tool_name, tool_digest) DO UPDATE
            SET effect = EXCLUDED.effect, group_key = EXCLUDED.group_key,
                reason = EXCLUDED.reason, last_seen_at = NOW()
            """,
            list(rows),
        )


async def list_curation_items(*, include_resolved: bool = False) -> list[dict[str, Any]]:
    async with get_db_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT * FROM mcp_curation_items
            WHERE %s OR resolved_at IS NULL
            ORDER BY brokerage, last_seen_at DESC
            """,
            (include_resolved,),
        )
        return list(await cur.fetchall())


async def resolve_curation_items(brokerage: str, tool_names: Sequence[str]) -> int:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE mcp_curation_items SET resolved_at = NOW()
            WHERE brokerage = %s AND tool_name = ANY(%s) AND resolved_at IS NULL
            """,
            (brokerage, list(tool_names)),
        )
        return cur.rowcount


async def reviewed_classifications(*, prompt_version: str) -> list[dict[str, Any]]:
    """Brokerage classifications a person marked reviewed, for the repo seed."""
    async with get_db_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT DISTINCT ON (scope_key, tool_name, tool_digest) *
            FROM mcp_tool_classifications
            WHERE scope_key LIKE 'brokerage:%%' AND prompt_version = %s
              AND reviewed_at IS NOT NULL
            ORDER BY scope_key, tool_name, tool_digest, reviewed_at DESC
            """,
            (prompt_version,),
        )
        return list(await cur.fetchall())


async def mark_reviewed(
    scope_key: str, tool_name: str, *, prompt_version: str, group_key: str | None
) -> int:
    """Stamp the newest classification of a tool reviewed, with the person's group."""
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE mcp_tool_classifications c SET reviewed_at = NOW(), group_key = %s
            WHERE (c.scope_key, c.tool_name, c.tool_digest, c.model, c.prompt_version) = (
                SELECT scope_key, tool_name, tool_digest, model, prompt_version
                FROM mcp_tool_classifications
                WHERE scope_key = %s AND tool_name = %s AND prompt_version = %s
                ORDER BY created_at DESC LIMIT 1
            )
            """,
            (group_key, scope_key, tool_name, prompt_version),
        )
        return cur.rowcount
