"""Which brokerage tools the layers below the code put in a group, per user.

The precedence, highest first:

1. ``UNCURATED`` in code: refused.
2. A group in ``brokerage_capabilities._CURATION``.
3. A reviewed row shipped in the repo seed (``seed.json``).
4. The user's own choice on the tools page: a group for a tool known to read,
   or off for any tool, which refuses it at every grant.
5. A generated classification: the model said read, the deterministic rules
   agree, and the model's confidence clears ``AUTO_ASSIGN_CONFIDENCE``.
6. Nothing: the tool stays ungrouped, and an ungrouped tool is callable.

The first two are applied by ``brokerage_capabilities`` itself, which refuses to
let anything here override them; this module builds the ``Assignments`` for the
rest. An assignment moves a tool into a group, where a grant that leaves the
group out denies it, or out of reach; it never makes anything callable that
was not. Every layer here is matched on the tool's current digest, read from
the user's own discovery snapshot, so a vendor that changes a tool loses its
grouping at that same moment rather than when a model next gets to it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.server.database import mcp_grouping as db
from src.server.database.pool import get_db_connection
from src.server.services.brokerage_capabilities import (
    Assignment,
    Assignments,
    group_keys_for,
    is_curated,
    vendor_for_url,
)
from src.server.services.egress import fold_tool_name
from src.server.services.mcp_grouping.digest import tool_digest
from src.server.services.mcp_grouping.rules import effect_floor
from src.server.services.mcp_grouping.schema import GROUPING_PROMPT_VERSION, Effect

logger = logging.getLogger(__name__)

# Chosen against the hand-curated IBKR, Webull, moomoo and Robinhood groupings
# with every tool treated as uncurated (scripts/mcp_grouping/validate_grouping.py):
# no run placed a read in the wrong group at 0.85 or above, and every mistake
# seen scored 0.80 or less. A wrong group puts a tool under consent the user
# meant for something else, while a read left ungrouped stays callable, so the
# margin is spent on precision.
AUTO_ASSIGN_CONFIDENCE = 0.85

_SEED_PATH = Path(__file__).with_name("seed.json")


def brokerage_scope(brokerage: str) -> str:
    return f"brokerage:{brokerage}"


def platform_model() -> str | None:
    """The model shared brokerage runs use, and so the one the gate reads rows of."""
    from src.server.app import setup
    from src.server.services.llm.config import slot_model

    if setup.agent_config is None or setup.agent_config.llm is None:
        return None
    return slot_model(setup.agent_config.llm, "background")


@dataclass(frozen=True)
class Verdict:
    """One classification as the gate reads it."""

    group: str | None
    effect: Effect
    confidence: float


def _load_seed(path: Path = _SEED_PATH) -> dict[str, dict[tuple[str, str], str]]:
    """brokerage -> (folded tool, digest) -> group, from the reviewed repo seed."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {
        brokerage: {
            (fold_tool_name(row["tool"]), row["tool_digest"]): row["group"]
            for row in rows
        }
        for brokerage, rows in (raw.get("brokerages") or {}).items()
    }


SEED = _load_seed()


def resolve(
    brokerage: str,
    tools: Iterable[dict[str, Any]],
    *,
    verdicts: Mapping[tuple[str, str], Verdict],
    choices: Mapping[str, str | None],
    seed: Mapping[tuple[str, str], str],
    threshold: float = AUTO_ASSIGN_CONFIDENCE,
) -> dict[str, Assignment]:
    """The assignments for one brokerage over the tools this user's snapshot holds.

    ``verdicts`` and ``seed`` are keyed (folded name, digest); ``choices`` by the
    tool name as the user chose it. Every layer re-reads the rules live: a
    stored verdict says what the tool was when it was classified, and the rules
    are cheap enough to ask again.
    """
    offered = set(group_keys_for(brokerage))
    folded_choices = {fold_tool_name(k): v for k, v in choices.items()}
    out: dict[str, Assignment] = {}
    for tool in tools:
        name = str(tool.get("name") or "")
        if not name or is_curated(brokerage, name):
            continue
        folded = fold_tool_name(name)
        key = (folded, tool_digest(tool))
        reads = effect_floor(tool)[0] is Effect.READ
        if reads and (group := seed.get(key)) in offered:
            out[folded] = Assignment(name, group, "seed")
            continue
        verdict = verdicts.get(key)
        known_read = reads and verdict is not None and verdict.effect is Effect.READ
        if folded in folded_choices:
            choice = folded_choices[folded]
            if choice is None:
                out[folded] = Assignment(name, None, "user")
            elif known_read and choice in offered:
                out[folded] = Assignment(name, choice, "user")
            # A choice the tool no longer qualifies for stands in for the
            # generated row rather than falling to it.
            continue
        if known_read:
            if verdict.group in offered and verdict.confidence >= threshold:
                out[folded] = Assignment(name, verdict.group, "generated")
        elif verdict is not None and (held := non_read_assignment(name, verdict)):
            out[folded] = held
    return out


def non_read_assignment(name: str, verdict: Verdict) -> Assignment | None:
    """What a classified tool that does not only read gets, if anything.

    Undecided on purpose: None leaves it ungrouped, so callable as if it had
    never been classified, and the worker has queued it for a person. The
    alternatives (the rung the model picked, or a refusal until a release
    curates it) narrow what a grant lets through, and either one returns its
    ``Assignment`` here.
    """
    return None


def can_choose(brokerage: str, tool: dict[str, Any], verdict: Verdict | None) -> bool:
    """Whether the tools page may offer the user a group for this tool: a read
    the code did not decide, by the model's word and the rules' both."""
    return (
        not is_curated(brokerage, str(tool.get("name") or ""))
        and verdict is not None
        and verdict.effect is Effect.READ
        and effect_floor(tool)[0] is Effect.READ
    )


async def verdicts_for(
    brokerages: Iterable[str], *, conn=None
) -> dict[str, dict[tuple[str, str], Verdict]]:
    """brokerage -> (folded name, digest) -> verdict, under the current model."""
    names = sorted(set(brokerages))
    model = platform_model()
    if not names or model is None:
        return {}
    rows = await db.classifications_for(
        [brokerage_scope(b) for b in names],
        prompt_version=GROUPING_PROMPT_VERSION,
        model=model,
        conn=conn,
    )
    out: dict[str, dict[tuple[str, str], Verdict]] = {}
    for (scope, tool, digest), row in rows.items():
        out.setdefault(scope.removeprefix("brokerage:"), {})[
            (fold_tool_name(tool), digest)
        ] = Verdict(row["group_key"], Effect(row["effect"]), float(row["confidence"]))
    return out


async def load_assignments(
    user_id: str, brokerages: Iterable[str], *, conn=None
) -> dict[str, Assignments]:
    """brokerage -> this user's assignments, for every brokerage asked about.

    Fails closed: a read that errors leaves the brokerage with none, which is
    the code's curation alone, the policy before this module existed.
    """
    wanted = {b for b in brokerages if b and group_keys_for(b)}
    if not wanted:
        return {}
    try:
        # A savepoint when the caller's transaction is lent, so a failed read
        # here cannot abort the grant writes that follow it there.
        async with get_db_connection(conn) as db_conn, db_conn.transaction():
            snapshots = await db.user_server_snapshots(user_id, conn=db_conn)
            verdicts = await verdicts_for(wanted, conn=db_conn)
            choices = await db.get_choices(user_id, sorted(wanted), conn=db_conn)
    except Exception:
        logger.warning(
            "[mcp_grouping] assignment read failed for %s; code curation only",
            user_id,
            exc_info=True,
        )
        return {}
    tools_by_vendor: dict[str, list[dict[str, Any]]] = {}
    for snap in snapshots:
        vendor = vendor_for_url(snap.get("url"))
        if vendor in wanted:
            tools_by_vendor.setdefault(vendor, []).extend(snap.get("tools") or [])
    return {
        vendor: resolve(
            vendor,
            tools,
            verdicts=verdicts.get(vendor, {}),
            choices=choices.get(vendor, {}),
            seed=SEED.get(vendor, {}),
        )
        for vendor, tools in tools_by_vendor.items()
    }
