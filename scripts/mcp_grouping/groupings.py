"""Brokerage tool groupings, for the person who curates them.

    uv run python scripts/mcp_grouping/groupings.py queue
    uv run python scripts/mcp_grouping/groupings.py review moomoo account_cash_logs --group account
    uv run python scripts/mcp_grouping/groupings.py review webull some_tool --refuse
    uv run python scripts/mcp_grouping/groupings.py export

``queue`` lists the tools the model looked at and the gate left ungrouped: anything
that is not a read, and reads with no confident group. An item whose tool is
now grouped in code is closed as it is listed. ``review`` stamps the newest
classification of a tool reviewed, with the group a person chose. ``export``
writes the reviewed read rows to the repo seed, which self-hosted installs load
below the code's own curation without running a model.

A tool that is not a read never goes to the seed: it is curated in
``brokerage_capabilities.py``, where an order tool also gets its adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")


async def _queue() -> int:
    from src.server.database import mcp_grouping as db
    from src.server.services.brokerage_capabilities import is_curated

    items = await db.list_curation_items()
    open_items = []
    for item in items:
        if is_curated(item["brokerage"], item["tool_name"]):
            await db.resolve_curation_items(item["brokerage"], [item["tool_name"]])
            continue
        open_items.append(item)
    if not open_items:
        print("Nothing waiting for curation.")
        return 0
    for item in open_items:
        print(
            f"{item['brokerage']:<10} {item['tool_name']:<40} {item['effect']:<14} "
            f"{item['group_key'] or '-':<14} {item['reason']}  "
            f"(last seen {item['last_seen_at']:%Y-%m-%d %H:%M})"
        )
    return 0


async def _review(brokerage: str, tool: str, group: str | None) -> int:
    from src.server.database import mcp_grouping as db
    from src.server.services.brokerage_capabilities import group_keys_for
    from src.server.services.mcp_grouping.assignments import brokerage_scope
    from src.server.services.mcp_grouping.schema import GROUPING_PROMPT_VERSION

    if group is not None and group not in group_keys_for(brokerage):
        print(f"{group!r} is not a group {brokerage} offers: {group_keys_for(brokerage)}")
        return 2
    count = await db.mark_reviewed(
        brokerage_scope(brokerage), tool, prompt_version=GROUPING_PROMPT_VERSION, group_key=group
    )
    if not count:
        print(f"No current classification for {brokerage}/{tool}.")
        return 1
    await db.resolve_curation_items(brokerage, [tool])
    print(f"Reviewed {brokerage}/{tool}: {group or 'refused'}")
    return 0


async def _export(path: str) -> int:
    from src.server.database import mcp_grouping as db
    from src.server.services.mcp_grouping.schema import GROUPING_PROMPT_VERSION

    rows = await db.reviewed_classifications(prompt_version=GROUPING_PROMPT_VERSION)
    seed: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row["effect"] != "read" or not row["group_key"]:
            continue
        seed.setdefault(row["scope_key"].removeprefix("brokerage:"), []).append(
            {"tool": row["tool_name"], "tool_digest": row["tool_digest"], "group": row["group_key"]}
        )
    for entries in seed.values():
        entries.sort(key=lambda e: e["tool"])
    with open(path, "w") as f:
        json.dump({"version": 1, "brokerages": dict(sorted(seed.items()))}, f, indent=2)
        f.write("\n")
    print(f"Wrote {sum(len(v) for v in seed.values())} rows to {path}")
    return 0


async def main() -> int:
    from src.server.database.pool import get_or_create_pool

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("queue")
    review = sub.add_parser("review")
    review.add_argument("brokerage")
    review.add_argument("tool")
    choice = review.add_mutually_exclusive_group(required=True)
    choice.add_argument("--group")
    choice.add_argument("--refuse", action="store_true")
    export = sub.add_parser("export")
    export.add_argument(
        "--out",
        default=os.path.join(
            os.path.dirname(__file__), "..", "..",
            "src", "server", "services", "mcp_grouping", "seed.json",
        ),
    )
    args = parser.parse_args()

    pool = get_or_create_pool()
    await pool.open()
    try:
        if args.command == "queue":
            return await _queue()
        if args.command == "review":
            return await _review(args.brokerage, args.tool, None if args.refuse else args.group)
        return await _export(os.path.abspath(args.out))
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
