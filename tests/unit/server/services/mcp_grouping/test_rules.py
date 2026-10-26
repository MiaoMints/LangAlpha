"""The deterministic floor holds every order and uncurated tool off the read path."""

from __future__ import annotations

import pytest

from src.server.services.brokerage_capabilities import UNCURATED, _ORDER_TOOLS
from src.server.services.mcp_grouping.rules import effect_floor
from src.server.services.mcp_grouping.schema import Effect


def _named(name: str) -> dict:
    return {"name": name, "description": "", "input_schema": {"type": "object"}}


@pytest.mark.parametrize(
    "name", sorted({n for tools in _ORDER_TOOLS.values() for n in tools})
)
def test_every_order_tool_floors_at_order_by_name_alone(name):
    assert effect_floor(_named(name))[0] is Effect.ORDER


@pytest.mark.parametrize("name", sorted({n for tools in UNCURATED.values() for n in tools}))
def test_every_uncurated_tool_floors_above_read(name):
    assert effect_floor(_named(name))[0] is not Effect.READ


@pytest.mark.parametrize(
    "name",
    [
        "get_account_orders",
        "get_order_status",
        "quote_option_exercise_probability",
        "sim_trade_max_buy_sell",
        "quote_financials_earnings_price_move",
        "whats_new",
        "get_insider_transactions",
    ],
)
def test_reads_stay_read(name):
    assert effect_floor(_named(name)) == (Effect.READ, None)


def test_side_alone_is_not_an_order():
    tool = {
        "name": "get_order_rules",
        "input_schema": {"type": "object", "properties": {"side": {}, "contract_id": {}}},
    }
    assert effect_floor(tool)[0] is Effect.READ


def test_side_and_size_together_make_an_order():
    tool = {
        "name": "estimate",
        "input_schema": {"type": "object", "properties": {"side": {}, "qty": {}}},
    }
    assert effect_floor(tool)[0] is Effect.ORDER


def test_destructive_hint_forces_order():
    tool = {**_named("get_quotes"), "annotations": {"destructiveHint": True}}
    assert effect_floor(tool) == (Effect.ORDER, "annotated destructive")


def test_read_only_hint_false_forces_at_least_account_write():
    tool = {**_named("get_quotes"), "annotations": {"readOnlyHint": False}}
    assert effect_floor(tool)[0].rank >= Effect.ACCOUNT_WRITE.rank


def test_read_only_hint_true_does_not_release_an_order_name():
    tool = {**_named("place_stock_order"), "annotations": {"readOnlyHint": True}}
    assert effect_floor(tool)[0] is Effect.ORDER
