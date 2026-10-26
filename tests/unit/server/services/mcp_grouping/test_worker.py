"""The worker classifies only what is missing, tightens with the rules, and
leaves what is not a confident read ungrouped for a person."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.server.services.mcp_grouping import worker
from src.server.services.mcp_grouping.digest import tool_digest
from src.server.services.mcp_grouping.schema import Effect, ToolGroup

NEW_READ = {"name": "account_cash_report", "description": "cash", "input_schema": {}}
ORDERISH = {
    "name": "account_combo_estimate",
    "description": "max qty",
    "input_schema": {"properties": {"side": {}, "qty": {}}},
}
CURATED = {"name": "quote_stock_quote", "description": "quote", "input_schema": {}}


class FakeDB:
    def __init__(self):
        self.classifications: list[dict] = []
        self.curation: list[dict] = []

    async def classifications_for(self, scopes, *, prompt_version, model, conn=None):
        return {
            (r["scope_key"], r["tool_name"], r["tool_digest"]): r for r in self.classifications
        }

    async def insert_classifications(self, rows):
        self.classifications.extend(rows)

    async def open_curation_items(self, rows):
        self.curation.extend(rows)


class FakeRunner:
    """Answers (effect, group, confidence) per tool; a tool missing from
    ``answers`` is left out, as a model may."""

    def __init__(self, answers: dict[str, tuple[Effect, str | None, float]]):
        self.answers = answers
        self.grouped: list[str] = []

    async def group_tools(self, complete, *, brokerage, tools):
        self.grouped += [t["name"] for t in tools]
        return [
            ToolGroup(name=t["name"], effect=effect, group=group, confidence=confidence)
            for t in tools
            if t["name"] in self.answers
            for effect, group, confidence in [self.answers[t["name"]]]
        ]


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    reapplied: list[str] = []
    monkeypatch.setattr(worker, "db", db)
    monkeypatch.setattr(worker, "platform_model", lambda: "platform-model")

    async def reapply(vendor, **_):
        reapplied.append(vendor)
        return 1

    monkeypatch.setattr(worker, "reapply_brokerage", reapply)
    return SimpleNamespace(db=db, reapplied=reapplied)


def _use_runner(monkeypatch, fake):
    import src.server.services.mcp_grouping as pkg

    monkeypatch.setattr(pkg, "runner", fake, raising=False)


def _job(tools, scope="brokerage:moomoo"):
    return {"job_id": 1, "scope_key": scope, "tools": tools, "server_name": "moomoo",
            "attempts": 1}


@pytest.mark.asyncio
async def test_a_job_classifies_only_what_the_code_does_not_decide(env, monkeypatch):
    fake = FakeRunner({
        "account_cash_report": (Effect.READ, "account", 0.95),
        "account_combo_estimate": (Effect.READ, "account", 0.99),
    })
    _use_runner(monkeypatch, fake)

    assert await worker.run_job(_job([NEW_READ, ORDERISH, CURATED]), object())

    assert sorted(fake.grouped) == ["account_cash_report", "account_combo_estimate"]
    rows = {r["tool_name"]: r for r in env.db.classifications}
    assert rows["account_cash_report"]["effect"] == "read"
    # The model said read; the rules see a side and a size, and the stricter wins.
    combo = rows["account_combo_estimate"]
    assert (combo["model_effect"], combo["effect"]) == ("read", "order")
    assert combo["rule"] == "takes a side and a size"
    assert all(r["model"] == "platform-model" for r in env.db.classifications)
    assert [c["tool_name"] for c in env.db.curation] == ["account_combo_estimate"]
    assert env.reapplied == ["moomoo"]


@pytest.mark.asyncio
async def test_a_tool_the_model_calls_a_write_goes_to_a_person(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.ACCOUNT_WRITE, "account", 0.99)})
    _use_runner(monkeypatch, fake)
    await worker.run_job(_job([NEW_READ]), object())
    row = env.db.classifications[0]
    assert (row["model_effect"], row["effect"], row["rule"]) == (
        "account_write", "account_write", None
    )
    assert env.db.curation[0]["reason"] == "account_write"


@pytest.mark.asyncio
async def test_a_confident_read_without_a_group_goes_to_a_person(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, None, 0.9)})
    _use_runner(monkeypatch, fake)
    await worker.run_job(_job([NEW_READ]), object())
    assert env.db.curation[0]["reason"].startswith("read, but no confident group")


@pytest.mark.asyncio
async def test_a_rerun_asks_about_nothing_already_answered(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, "account", 0.9)})
    _use_runner(monkeypatch, fake)
    await worker.run_job(_job([NEW_READ]), object())
    fake.grouped.clear()
    env.reapplied.clear()

    assert not await worker.run_job(_job([NEW_READ]), object())
    assert (fake.grouped, env.reapplied) == ([], [])


@pytest.mark.asyncio
async def test_a_changed_tool_is_asked_about_again(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, "account", 0.9)})
    _use_runner(monkeypatch, fake)
    await worker.run_job(_job([NEW_READ]), object())
    changed = {**NEW_READ, "description": "cash, now with transfers"}
    fake.grouped.clear()
    await worker.run_job(_job([changed]), object())
    assert fake.grouped == ["account_cash_report"]
    assert {r["tool_digest"] for r in env.db.classifications} == {
        tool_digest(NEW_READ), tool_digest(changed)
    }


@pytest.mark.asyncio
async def test_what_the_model_left_out_raises_after_keeping_the_rest(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, "account", 0.9)})
    _use_runner(monkeypatch, fake)
    with pytest.raises(worker.Incomplete):
        await worker.run_job(_job([NEW_READ, ORDERISH]), object())
    assert [r["tool_name"] for r in env.db.classifications] == ["account_cash_report"]
    assert env.reapplied == ["moomoo"]
