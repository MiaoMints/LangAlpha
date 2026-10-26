"""Deterministic floors on a tool's effect, read off what the vendor published.

A model reads a tool's description and says what it does; these rules read its
name, its arguments and its annotations and say what it *at least* does. The
gate takes the stricter of the two, so a rule can hold back a tool the model
called a read and can never release one the model held. That is the point of
having both: the model is the one that can read prose, and the rules are the
part that cannot be talked out of a verdict by a description.

The rules are written against what brokers actually ship, and every order tool
we curate by hand is pinned to come out ``order`` here by name or arguments
alone, whatever a model later says about it.

Two calls are deliberate. A ``side`` argument alone is not an order: brokers
put it on read filters (insider transactions, order history, IBKR's order
rules), so it takes a side and a size together. And the word ``order`` in a
name is not an order either, since most of a broker's order tools read orders
that already exist; a name has to pair it with a verb that writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from src.server.services.mcp_enrichment.schema import Effect

# Verbs that, anywhere in a name, mean an order is placed, changed, ended or
# released. ``confirm`` and ``answer`` release an order a broker held back
# with a question.
_ORDER_VERBS = frozenset(
    {
        "place",
        "submit",
        "cancel",
        "modify",
        "replace",
        "amend",
        "revoke",
        "confirm",
        "answer",
    }
)
# The same, but only leading a name: later in one they are as often nouns, as
# in moomoo's ``quote_option_exercise_probability`` and
# ``sim_trade_max_buy_sell``, both reads.
_LEADING_ORDER_VERBS = frozenset({"exercise", "buy", "sell", "close", "liquidate"})
# Verbs that write something. On their own they make an account write; next
# to an order noun they make an order, and next to a post noun a public post.
_WRITE_VERBS = frozenset(
    {
        "create",
        "update",
        "delete",
        "add",
        "remove",
        "set",
        "edit",
        "rename",
        "follow",
        "unfollow",
        "save",
        "upload",
        "enable",
        "disable",
        "toggle",
        "subscribe",
        "unsubscribe",
        "reset",
        "clear",
        "mark",
        "archive",
        "register",
        "link",
        "unlink",
        "input",
        "send",
        "put",
        "provide",
        "publish",
        "reply",
        "write",
        "transfer",
        "withdraw",
        "deposit",
    }
)
_ORDER_NOUNS = frozenset({"order", "orders", "instruction", "instructions", "trade", "trades"})
_POST_NOUNS = frozenset({"post", "posts", "comment", "comments", "feedback", "message", "tweet"})

# Argument names that carry an order on their own.
_ORDER_ARGS = frozenset(
    {"order", "orders", "new_order", "new_orders", "instruction", "instructions", "order_request"}
)
_SIDE_ARGS = frozenset({"side", "trd_side", "order_side", "buy_sell"})
_SIZE_ARGS = frozenset(
    {
        "qty",
        "quantity",
        "size",
        "cash_size",
        "cash_order_qty",
        "new_qty",
        "notional",
        "shares",
        "amount",
    }
)
# Write verbs that only lead a name, for the same reason: IBKR's ``whats_new``
# and moomoo's ``quote_financials_earnings_price_move`` read.
_LEADING_WRITE_VERBS = frozenset({"new", "move"})
_MAX_SCHEMA_DEPTH = 6


def _tokens(name: str) -> list[str]:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [t for t in re.split(r"[^a-z0-9]+", snake.lower()) if t]


def _fold(name: str) -> str:
    return "_".join(_tokens(name))


def _properties(schema: Any, depth: int = 0) -> Iterator[str]:
    """Every property name in a schema, nested objects and array items included."""
    if depth > _MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
        return
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            yield _fold(str(name))
            yield from _properties(sub, depth + 1)
    for key in ("items", "additionalProperties"):
        yield from _properties(schema.get(key), depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or ():
            yield from _properties(sub, depth + 1)


def effect_floor(tool: dict[str, Any]) -> tuple[Effect, str | None]:
    """The least this tool can be, and the rule that said so (None for a read)."""
    annotations = tool.get("annotations") or {}
    if annotations.get("destructiveHint") is True:
        return Effect.ORDER, "annotated destructive"

    words = _tokens(str(tool.get("name") or ""))
    tokens = set(words)
    leading = set(words[:1])
    if verb := tokens & _ORDER_VERBS or leading & _LEADING_ORDER_VERBS:
        return Effect.ORDER, f"name says {sorted(verb)[0]}"
    writes = tokens & _WRITE_VERBS or leading & _LEADING_WRITE_VERBS
    if writes and tokens & _ORDER_NOUNS:
        return Effect.ORDER, "name writes an order"

    schema = tool.get("input_schema") or {}
    if arg := set(_properties(schema)) & _ORDER_ARGS:
        return Effect.ORDER, f"takes {sorted(arg)[0]}"
    # Top level only: a quote for a combo takes legs that each have a side and
    # a size, and quoting one places nothing.
    top = {_fold(str(n)) for n in (schema.get("properties") or {})}
    if top & _SIDE_ARGS and top & _SIZE_ARGS:
        return Effect.ORDER, "takes a side and a size"

    if writes and tokens & _POST_NOUNS:
        return Effect.PUBLIC_POST, "name publishes"
    if writes:
        return Effect.ACCOUNT_WRITE, f"name says {sorted(writes)[0]}"
    if annotations.get("readOnlyHint") is False:
        return Effect.ACCOUNT_WRITE, "annotated not read-only"
    return Effect.READ, None
