"""The precedence below the code: seed, the user's choice, a generated row.

Every assertion here is about what the relay will end up enforcing, since
``denied_tools`` is what the denial it reads is written from. An assignment
only ever narrows: with none, a tool the code names nowhere stays callable.
"""

from __future__ import annotations

import pytest

from src.server.services import brokerage_capabilities as bc
from src.server.services.brokerage_capabilities import Assignment
from src.server.services.egress import fold_tool_name
from src.server.services.mcp_grouping.assignments import Verdict, can_choose, resolve
from src.server.services.mcp_grouping.digest import tool_digest
from src.server.services.mcp_grouping.schema import Effect


def _tool(name: str, **schema_props) -> dict:
    return {
        "name": name,
        "description": f"Returns {name}",
        "input_schema": {"type": "object", "properties": schema_props},
    }


def _assigned(*entries: tuple[str, str | None, str]) -> dict[str, Assignment]:
    return {fold_tool_name(t): Assignment(t, g, s) for t, g, s in entries}


NEW_READ = _tool("account_cash_report", acc_id={"type": "string"})
NEW_READ_2 = _tool("account_fee_report", acc_id={"type": "string"})
ORDERISH = _tool("account_combo_estimate", side={"type": "string"}, qty={"type": "number"})


def _key(tool: dict) -> tuple[str, str]:
    return (fold_tool_name(tool["name"]), tool_digest(tool))


class TestToolsForWithAssignments:
    def test_an_assigned_tool_is_denied_where_its_group_is_not_granted(self):
        assigned = _assigned(("account_cash_report", "account", "generated"))
        assert "account_cash_report" in bc.denied_tools("moomoo", ["market_data"], assigned)
        assert "account_cash_report" not in bc.denied_tools("moomoo", ["account"], assigned)
        assert "account_cash_report" in bc.tools_for("moomoo", ["account"], assigned)

    def test_without_an_assignment_the_new_tool_stays_callable(self):
        assert "account_cash_report" not in bc.denied_tools("moomoo", ["market_data"])
        assert not bc.is_always_denied("moomoo", "account_cash_report")
        assert bc.group_of_tool("moomoo", "account_cash_report") is None

    def test_a_users_refusal_denies_the_tool_at_every_grant(self):
        assigned = _assigned(("account_cash_report", None, "user"))
        everything = bc.group_keys_for("moomoo")
        assert "account_cash_report" in bc.denied_tools("moomoo", everything, assigned)
        assert "account_cash_report" not in bc.tools_for("moomoo", everything, assigned)
        assert bc.is_always_denied("moomoo", "account_cash_report", assigned)

    def test_an_assignment_never_moves_a_curated_tool(self):
        # The code puts quote_stock_quote in market_data; an assignment to
        # account must not make it reachable through an account grant alone.
        assigned = _assigned(("quote_stock_quote", "account", "user"))
        assert "quote_stock_quote" not in bc.tools_for("moomoo", ["account"], assigned)
        assert bc.group_of_tool("moomoo", "quote_stock_quote", assigned) == "market_data"

    def test_an_assignment_never_releases_an_uncurated_tool(self):
        assigned = _assigned(("place_event_instruction", "staged_orders", "generated"))
        assert "place_event_instruction" not in bc.tools_for(
            "webull", ["account", "staged_orders"], assigned
        )
        assert bc.is_always_denied("webull", "place_event_instruction", assigned)

    def test_a_group_the_brokerage_does_not_offer_is_ignored(self):
        # moomoo offers no scanners group.
        assigned = _assigned(("account_cash_report", "scanners", "generated"))
        assert bc.group_of_tool("moomoo", "account_cash_report", assigned) is None
        assert "account_cash_report" not in bc.denied_tools("moomoo", ["account"], assigned)

    def test_assignments_are_matched_folded(self):
        assigned = _assigned(("Account_Cash_Report", "account", "generated"))
        assert bc.group_of_tool("moomoo", "account_cash_report", assigned) == "account"

    def test_an_assigned_tool_binds_like_its_group(self):
        from src.server.services.tool_binding import BindingInputs, resolve_plan

        assigned = _assigned(("account_cash_report", "account", "generated"))
        plan = resolve_plan("moomoo", ["account"], BindingInputs(), assigned=assigned)
        assert "account_cash_report" in plan.by_tool
        assert plan.by_tool["account_cash_report"].binding == "ptc"


class TestResolve:
    def _verdict(self, group="account", effect=Effect.READ, confidence=0.95):
        return Verdict(group, effect, confidence)

    def test_a_confident_read_is_assigned_generated(self):
        out = resolve(
            "moomoo", [NEW_READ], verdicts={_key(NEW_READ): self._verdict()},
            choices={}, seed={},
        )
        assert out[fold_tool_name("account_cash_report")] == Assignment(
            "account_cash_report", "account", "generated"
        )

    @pytest.mark.parametrize("confidence", [0.0, 0.5, 0.79])
    def test_below_the_threshold_nothing_is_assigned(self, confidence):
        out = resolve(
            "moomoo", [NEW_READ],
            verdicts={_key(NEW_READ): self._verdict(confidence=confidence)},
            choices={}, seed={}, threshold=0.8,
        )
        assert out == {}

    @pytest.mark.parametrize("effect", [Effect.ACCOUNT_WRITE, Effect.PUBLIC_POST, Effect.ORDER])
    def test_a_verdict_that_is_not_a_read_is_never_assigned(self, effect):
        out = resolve(
            "moomoo", [NEW_READ],
            verdicts={_key(NEW_READ): self._verdict(effect=effect, confidence=1.0)},
            choices={}, seed={},
        )
        assert out == {}

    def test_the_rules_hold_back_a_tool_a_stored_verdict_calls_a_read(self):
        # Even a seed row and a user choice: the floor is read live every time.
        key = _key(ORDERISH)
        out = resolve(
            "moomoo", [ORDERISH],
            verdicts={key: self._verdict(confidence=1.0)},
            choices={"account_combo_estimate": "account"},
            seed={key: "account"},
        )
        assert out == {}

    def test_a_changed_tool_loses_its_grouping_at_once(self):
        changed = {**NEW_READ, "description": "Now also places a transfer"}
        out = resolve(
            "moomoo", [changed], verdicts={_key(NEW_READ): self._verdict()},
            choices={}, seed={_key(NEW_READ): "account"},
        )
        assert out == {}

    def test_seed_outranks_the_users_choice_and_the_model(self):
        out = resolve(
            "moomoo", [NEW_READ],
            verdicts={_key(NEW_READ): self._verdict(group="market_data")},
            choices={"account_cash_report": "market_data"},
            seed={_key(NEW_READ): "account"},
        )
        assert out[fold_tool_name("account_cash_report")].source == "seed"
        assert out[fold_tool_name("account_cash_report")].group == "account"

    def test_the_users_choice_outranks_the_model_even_below_the_threshold(self):
        out = resolve(
            "moomoo", [NEW_READ],
            verdicts={_key(NEW_READ): self._verdict(group=None, confidence=0.3)},
            choices={"account_cash_report": "market_data"}, seed={},
        )
        assert out[fold_tool_name("account_cash_report")] == Assignment(
            "account_cash_report", "market_data", "user"
        )

    def test_a_users_refusal_stands_in_for_the_generated_row(self):
        out = resolve(
            "moomoo", [NEW_READ, NEW_READ_2],
            verdicts={
                _key(NEW_READ): self._verdict(),
                _key(NEW_READ_2): self._verdict(),
            },
            choices={"account_cash_report": None}, seed={},
        )
        assert out == {
            fold_tool_name("account_cash_report"): Assignment(
                "account_cash_report", None, "user"
            ),
            fold_tool_name("account_fee_report"): Assignment(
                "account_fee_report", "account", "generated"
            ),
        }

    def test_a_refusal_holds_for_a_tool_that_does_not_only_read(self):
        out = resolve(
            "moomoo", [ORDERISH], verdicts={}, choices={"account_combo_estimate": None},
            seed={},
        )
        assert out == {
            fold_tool_name("account_combo_estimate"): Assignment(
                "account_combo_estimate", None, "user"
            )
        }

    def test_a_choice_needs_a_verdict_that_says_read(self):
        for verdicts in ({}, {_key(NEW_READ): self._verdict(effect=Effect.ACCOUNT_WRITE)}):
            out = resolve(
                "moomoo", [NEW_READ], verdicts=verdicts,
                choices={"account_cash_report": "account"}, seed={},
            )
            assert out == {}

    def test_curated_tools_are_skipped(self):
        tool = _tool("quote_stock_quote")
        out = resolve(
            "moomoo", [tool], verdicts={_key(tool): self._verdict()}, choices={}, seed={},
        )
        assert out == {}

    def test_can_choose_is_a_read_by_both_the_model_and_the_rules(self):
        assert can_choose("moomoo", NEW_READ, self._verdict(group=None, confidence=0.1))
        assert not can_choose("moomoo", NEW_READ, None)
        assert not can_choose("moomoo", ORDERISH, self._verdict())
        assert not can_choose("moomoo", _tool("quote_stock_quote"), self._verdict())
