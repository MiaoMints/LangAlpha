"""The tools page's view of brokerage groupings, and the user's per-tool choice.

A user may put a read tool the code did not decide into one of the
brokerage's groups, or refuse it, and nothing else: a tool that writes waits
for curation in code, where an order tool also gets its adapter.
"""

from __future__ import annotations

import logging
from typing import Any

from src.server.database import mcp_grouping as db
from src.server.services.brokerage_capabilities import (
    NO_ASSIGNMENTS,
    Assignments,
    group_keys_for,
    group_of_tool,
    is_curated,
)
from src.server.services.egress import fold_tool_name
from src.server.services.mcp_grouping.apply import reapply_brokerage
from src.server.services.mcp_grouping.assignments import (
    can_choose,
    load_assignments,
    verdicts_for,
)
from src.server.services.mcp_grouping.digest import tool_digest

logger = logging.getLogger(__name__)


class ChoiceRefused(ValueError):
    """The choice is not one this tool can take; the message says why."""


async def grouping_view(
    user_id: str, vendor: str | None, tools: list[dict[str, Any]]
) -> tuple[Assignments, dict[str, dict[str, Any]]]:
    """This user's assignments at the brokerage, and tool name -> the grouping
    fields the tools endpoint adds to each tool.

    ``capability_source`` says who put the tool in its group: ``code``, ``seed``,
    ``user`` or ``generated`` (the page's "Grouped automatically"), or None.
    ``choosable`` is whether the page may offer a group, and ``choice`` the
    user's stored one (``{"group": None}`` for a refusal), or None.
    """
    if not vendor or not group_keys_for(vendor):
        return NO_ASSIGNMENTS, {}
    try:
        assigned = (await load_assignments(user_id, [vendor])).get(
            vendor, NO_ASSIGNMENTS
        )
        verdicts = (await verdicts_for([vendor])).get(vendor, {})
        stored = {
            fold_tool_name(k): v
            for k, v in (await db.get_choices(user_id, [vendor])).get(vendor, {}).items()
        }
    except Exception:
        # The listing still answers, drawing what only the code groups, which
        # is also what the gate falls back to when these reads fail.
        logger.warning("[mcp_grouping] grouping read failed for %s", user_id, exc_info=True)
        return NO_ASSIGNMENTS, {}
    out: dict[str, dict[str, Any]] = {}
    for tool in tools:
        name = str(tool.get("name") or "")
        folded = fold_tool_name(name)
        verdict = verdicts.get((folded, tool_digest(tool)))
        hit = assigned.get(folded)
        out[name] = {
            "capability_source": (
                "code" if is_curated(vendor, name) and group_of_tool(vendor, name)
                else hit.source if hit else None
            ),
            "effect": verdict.effect.value if verdict else None,
            "choosable": can_choose(vendor, tool, verdict),
            "choice": {"group": stored[folded]} if folded in stored else None,
        }
    return assigned, out


async def choose_group(
    user_id: str,
    vendor: str,
    tool: dict[str, Any],
    group: str | None,
) -> None:
    """Store the user's group (None refuses) and put it into force."""
    name = str(tool.get("name") or "")
    if is_curated(vendor, name):
        raise ChoiceRefused(f"{name!r} is grouped by LangAlpha and cannot be changed")
    if group is not None and group not in group_keys_for(vendor):
        raise ChoiceRefused(f"{group!r} is not a group this brokerage offers")
    verdict = (await verdicts_for([vendor])).get(vendor, {}).get(
        (fold_tool_name(name), tool_digest(tool))
    )
    if group is not None and not can_choose(vendor, tool, verdict):
        raise ChoiceRefused(f"{name!r} is not known to only read, so it waits for review")
    await db.set_choice(user_id, vendor, name, group)
    await reapply_brokerage(vendor, user_id=user_id)


async def clear_choice(user_id: str, vendor: str, name: str) -> None:
    await db.clear_choice(user_id, vendor, name)
    await reapply_brokerage(vendor, user_id=user_id)
