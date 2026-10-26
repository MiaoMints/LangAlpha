"""The worker: claims queued jobs and runs both stages, in every worker.

Nothing is kept between passes and nothing is consulted from process memory.
A job is a Postgres row claimed under its scope's advisory lock with a lease
(``mcp_enrichment.claim_job``), so a second worker never runs the same scope at
once, and a worker that dies mid-job leaves the row for another to claim when
the lease runs out. Results are keyed by content, so a job run twice writes
nothing the second time.

A job only adds rows for what is missing under the model and prompt version in
force: for a shipped brokerage, a classification for each tool the code does
not decide, and for every server, its description. When a brokerage gets new
rows, every connection at it has its allowlist rewritten and its owner's
workspaces re-resolve; a user's own server only needs the re-resolve, since
its description reaches nothing but prompt text.
"""

from __future__ import annotations

import asyncio
import logging
import random
from functools import partial
from typing import Any, Optional

from src.config.settings import get_mcp_enrichment_config
from src.server.database import mcp_enrichment as db
from src.server.database.mcp_config_version import bump_user_workspaces_mcp_version
from src.server.services.brokerages import brokerage_by_name
from src.server.services.brokerage_capabilities import is_curated
from src.server.services.mcp_enrichment.apply import reapply_brokerage
from src.server.services.mcp_enrichment.assignments import (
    AUTO_ASSIGN_CONFIDENCE,
    platform_model,
)
from src.server.services.mcp_enrichment.digest import (
    described_tools,
    server_digest,
    tool_digest,
)
from src.server.services.mcp_enrichment.rules import effect_floor
from src.server.services.mcp_enrichment.schema import (
    GROUPING_PROMPT_VERSION,
    PROFILE_PROMPT_VERSION,
    Effect,
)
from src.utils.concurrency import cancel_and_join

logger = logging.getLogger(__name__)

_LOG = "[McpEnrichment]"
STOP_GRACE = 30.0
# Backoff after a failed run, by attempt: a model outage clears in minutes, a
# tool list a model cannot describe will not clear at all.
_RETRY_AFTER_S = (60, 300, 1800, 7200)


class Incomplete(RuntimeError):
    """The model left some tools out; what it did answer is kept."""


async def _complete(llm_service: Any, user_id: str | None, system: str, user: str, schema):
    return await llm_service.complete(
        user_id=user_id,
        system_prompt=system,
        user_prompt=user,
        response_schema=schema,
        slot="background",
    )


async def run_job(job: dict[str, Any], llm_service: Any) -> bool:
    """Run one job's missing stages. Returns whether anything new was written.

    Grouping runs first, since it is what the gate waits on. Whatever lands is
    put into force even when a later step fails, and a job the model left
    tools out of raises :class:`Incomplete` so it backs off and the rerun asks
    only about what is still missing.
    """
    from src.server.services.mcp_enrichment import runner

    scope = job["scope_key"]
    tools: list[dict[str, Any]] = list(job["tools"] or [])
    brokerage = scope.removeprefix("brokerage:") if scope.startswith("brokerage:") else None
    user_id = None if brokerage else job["user_id"]
    model = platform_model() if brokerage else await llm_service.effective_model(user_id=user_id)
    if model is None:
        raise RuntimeError("no platform model is configured")
    complete = partial(_complete, llm_service, user_id)
    label = brokerage_by_name(brokerage).label if brokerage else job["server_name"]
    wrote = False
    grouped_all = True

    try:
        if brokerage:
            digests = {t["name"]: tool_digest(t) for t in tools}
            classified = await db.classifications_for(
                [scope], prompt_version=GROUPING_PROMPT_VERSION, model=model
            )
            pending = [
                t
                for t in tools
                if not is_curated(brokerage, t["name"])
                and (scope, t["name"], digests[t["name"]]) not in classified
            ]
            done = (
                await _classify(complete, brokerage, scope, model, pending, digests)
                if pending
                else set()
            )
            wrote = bool(done)
            grouped_all = len(done) == len(pending)

        described = described_tools(brokerage, tools)
        surface = server_digest(described)
        if not await db.has_profile(
            scope, surface, model=model, prompt_version=PROFILE_PROMPT_VERSION
        ):
            profile = await runner.profile_server(
                complete, server_label=label, tools=described
            )
            await db.insert_profile(
                scope_key=scope,
                server_digest=surface,
                model=model,
                prompt_version=PROFILE_PROMPT_VERSION,
                description=profile.description,
            )
            wrote = True
    finally:
        if wrote:
            if brokerage:
                await reapply_brokerage(brokerage)
            elif user_id:
                await bump_user_workspaces_mcp_version(user_id)
    if not grouped_all:
        raise Incomplete("the model left some tools ungrouped")
    return wrote


async def _classify(
    complete,
    brokerage: str,
    scope: str,
    model: str,
    tools: list[dict[str, Any]],
    digests: dict[str, str],
) -> set[str]:
    """Grouping for these tools, with the rules applied and holds queued for a person.

    Returns the names the model answered for."""
    from src.server.services.mcp_enrichment import runner

    by_name = {t["name"]: t for t in tools}
    groups = await runner.group_tools(complete, brokerage=brokerage, tools=tools)
    rows, held = [], []
    for g in groups:
        floor, rule = effect_floor(by_name[g.name])
        effect = g.effect.at_least(floor)
        rows.append(
            {
                "scope_key": scope,
                "tool_name": g.name,
                "tool_digest": digests[g.name],
                "model": model,
                "prompt_version": GROUPING_PROMPT_VERSION,
                "group_key": g.group,
                "effect": effect.value,
                "model_effect": g.effect.value,
                "rule": rule,
                "confidence": g.confidence,
                "reason": g.reason,
            }
        )
        if effect is not Effect.READ:
            why = f"{effect.value}" + (f" ({rule})" if rule else "")
        elif g.group is None or g.confidence < AUTO_ASSIGN_CONFIDENCE:
            why = f"read, but no confident group ({g.confidence:.2f})"
        else:
            continue
        held.append(
            {
                "brokerage": brokerage,
                "tool_name": g.name,
                "tool_digest": digests[g.name],
                "effect": effect.value,
                "group_key": g.group,
                "reason": why,
            }
        )
        logger.warning(f"{_LOG} held {brokerage}/{g.name} for curation: {why}")
    await db.insert_classifications(rows)
    if held:
        await db.open_curation_items(held)
    return {r["tool_name"] for r in rows}


class McpEnrichmentWorker:
    _instance: Optional["McpEnrichmentWorker"] = None

    def __init__(self) -> None:
        self._loop_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    @classmethod
    def get_instance(cls) -> "McpEnrichmentWorker":
        if cls._instance is None:
            cls._instance = McpEnrichmentWorker()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def start(self) -> None:
        if not get_mcp_enrichment_config().enabled:
            return
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._loop(), name="mcp-enrichment-worker")
        logger.info(f"{_LOG} started")

    async def stop(self) -> None:
        if self._loop_task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._loop_task, timeout=STOP_GRACE)
        except TimeoutError:
            await cancel_and_join(self._loop_task)
        except Exception:
            logger.warning(f"{_LOG} loop ended with an error at shutdown", exc_info=True)
        self._loop_task = None
        self._stop_event = asyncio.Event()

    async def _loop(self) -> None:
        interval = get_mcp_enrichment_config().interval_seconds
        while not self._stop_event.is_set():
            try:
                while not self._stop_event.is_set() and await self.run_once():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"{_LOG} pass failed", exc_info=True)
            jitter = interval * (0.8 + 0.4 * random.random())
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=jitter)
                return
            except TimeoutError:
                continue

    async def run_once(self) -> bool:
        """Claim and run one job. Returns whether there was one."""
        from src.server.app import setup

        if setup.llm_service is None:
            return False
        job = await db.claim_job()
        if job is None:
            return False
        try:
            await run_job(job, setup.llm_service)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            attempt = int(job["attempts"])
            retry = _RETRY_AFTER_S[min(attempt, len(_RETRY_AFTER_S)) - 1]
            logger.warning(
                f"{_LOG} job {job['job_id']} ({job['scope_key']}) attempt {attempt} "
                f"failed, retrying in {retry}s: {e}",
                exc_info=not isinstance(e, Incomplete),
            )
            await db.fail_job(job["job_id"], str(e), retry_after_s=retry)
            return True
        await db.finish_job(job["job_id"])
        logger.info(f"{_LOG} job {job['job_id']} ({job['scope_key']}) done")
        return True
