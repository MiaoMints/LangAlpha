"""The worker writes only what is missing, tightens with the rules, and holds
what is not a read for a person."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.server.services.mcp_enrichment import worker
from src.server.services.mcp_enrichment.digest import server_digest, tool_digest
from src.server.services.mcp_enrichment.schema import Effect, ServerProfile, ToolGroup

NEW_READ = {"name": "account_cash_report", "description": "cash", "input_schema": {}}
ORDERISH = {
    "name": "account_combo_estimate",
    "description": "max qty",
    "input_schema": {"properties": {"side": {}, "qty": {}}},
}
CURATED = {"name": "quote_stock_quote", "description": "quote", "input_schema": {}}
WEBULL_CURATED = {"name": "get_pending_instruction", "description": "pending", "input_schema": {}}
REFUSED = {"name": "place_event_instruction", "description": "order", "input_schema": {}}


class FakeDB:
    def __init__(self):
        self.classifications: list[dict] = []
        self.profiles: list[dict] = []
        self.curation: list[dict] = []

    async def classifications_for(self, scopes, *, prompt_version, model, conn=None):
        return {
            (r["scope_key"], r["tool_name"], r["tool_digest"]): r for r in self.classifications
        }

    async def insert_classifications(self, rows):
        self.classifications.extend(rows)

    async def has_profile(self, scope, digest, *, model, prompt_version, conn=None):
        return any(p["server_digest"] == digest for p in self.profiles)

    async def insert_profile(self, **row):
        self.profiles.append(row)

    async def open_curation_items(self, rows):
        self.curation.extend(rows)


class FakeRunner:
    """Answers (effect, group, confidence) per tool; a tool missing from
    ``answers`` is left out, as a model may."""

    def __init__(self, answers: dict[str, tuple[Effect, str | None, float]]):
        self.answers = answers
        self.grouped: list[str] = []
        self.profiled = 0
        self.described: list[str] = []
        self.profile_error: Exception | None = None

    async def profile_server(self, complete, *, server_label, tools):
        self.profiled += 1
        self.described = [t["name"] for t in tools]
        if self.profile_error:
            raise self.profile_error
        return ServerProfile(description="d")

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
    bumped: list[str] = []
    monkeypatch.setattr(worker, "db", db)
    monkeypatch.setattr(worker, "platform_model", lambda: "platform-model")

    async def reapply(vendor, **_):
        reapplied.append(vendor)
        return 1

    async def bump(user_id):
        bumped.append(user_id)
        return 1

    monkeypatch.setattr(worker, "reapply_brokerage", reapply)
    monkeypatch.setattr(worker, "bump_user_workspaces_mcp_version", bump)
    return SimpleNamespace(db=db, reapplied=reapplied, bumped=bumped)


def _use_runner(monkeypatch, fake):
    import src.server.services.mcp_enrichment as pkg

    monkeypatch.setattr(pkg, "runner", fake, raising=False)


def _job(tools, scope="brokerage:moomoo", user_id=None, server="moomoo"):
    return {"job_id": 1, "scope_key": scope, "tools": tools, "user_id": user_id,
            "server_name": server, "attempts": 1}


class _LLM:
    async def effective_model(self, *, user_id, slot="background"):
        return "user-model"


@pytest.mark.asyncio
async def test_a_brokerage_job_classifies_only_what_the_code_does_not_decide(env, monkeypatch):
    fake = FakeRunner({
        "account_cash_report": (Effect.READ, "account", 0.95),
        "account_combo_estimate": (Effect.READ, "account", 0.99),
    })
    _use_runner(monkeypatch, fake)

    assert await worker.run_job(_job([NEW_READ, ORDERISH, CURATED]), _LLM())

    assert sorted(fake.grouped) == ["account_cash_report", "account_combo_estimate"]
    rows = {r["tool_name"]: r for r in env.db.classifications}
    assert rows["account_cash_report"]["effect"] == "read"
    # The model said read; the rules see a side and a size, and the stricter wins.
    combo = rows["account_combo_estimate"]
    assert (combo["model_effect"], combo["effect"]) == ("read", "order")
    assert combo["rule"] == "takes a side and a size"
    assert all(r["model"] == "platform-model" for r in env.db.classifications)
    assert [c["tool_name"] for c in env.db.curation] == ["account_combo_estimate"]
    assert [p["description"] for p in env.db.profiles] == ["d"]
    assert env.reapplied == ["moomoo"]


@pytest.mark.asyncio
async def test_a_tool_the_model_calls_a_write_is_held_whatever_its_group(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.ACCOUNT_WRITE, "account", 0.99)})
    _use_runner(monkeypatch, fake)
    await worker.run_job(_job([NEW_READ]), _LLM())
    row = env.db.classifications[0]
    assert (row["model_effect"], row["effect"], row["rule"]) == (
        "account_write", "account_write", None
    )
    assert env.db.curation[0]["reason"] == "account_write"


@pytest.mark.asyncio
async def test_a_confident_read_without_a_group_is_held_for_a_person(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, None, 0.9)})
    _use_runner(monkeypatch, fake)
    await worker.run_job(_job([NEW_READ]), _LLM())
    assert env.db.curation[0]["reason"].startswith("read, but no confident group")


@pytest.mark.asyncio
async def test_the_description_never_covers_a_tool_no_grant_can_call(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, "account", 0.95)})
    _use_runner(monkeypatch, fake)

    job = _job([NEW_READ, WEBULL_CURATED, REFUSED], scope="brokerage:webull", server="webull")
    await worker.run_job(job, _LLM())

    assert sorted(fake.described) == ["account_cash_report", "get_pending_instruction"]
    assert env.db.profiles[0]["server_digest"] == server_digest([NEW_READ, WEBULL_CURATED])


@pytest.mark.asyncio
async def test_a_rerun_asks_about_nothing_already_answered(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, "account", 0.9)})
    _use_runner(monkeypatch, fake)
    await worker.run_job(_job([NEW_READ]), _LLM())
    fake.grouped.clear()
    fake.profiled = 0
    env.reapplied.clear()

    assert not await worker.run_job(_job([NEW_READ]), _LLM())
    assert (fake.grouped, fake.profiled, env.reapplied) == ([], 0, [])


@pytest.mark.asyncio
async def test_a_changed_tool_is_asked_about_again(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, "account", 0.9)})
    _use_runner(monkeypatch, fake)
    await worker.run_job(_job([NEW_READ]), _LLM())
    changed = {**NEW_READ, "description": "cash, now with transfers"}
    fake.grouped.clear()
    await worker.run_job(_job([changed]), _LLM())
    assert fake.grouped == ["account_cash_report"]
    assert {r["tool_digest"] for r in env.db.classifications} == {
        tool_digest(NEW_READ), tool_digest(changed)
    }


@pytest.mark.asyncio
async def test_what_the_model_left_out_raises_after_keeping_the_rest(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, "account", 0.9)})
    _use_runner(monkeypatch, fake)
    with pytest.raises(worker.Incomplete):
        await worker.run_job(_job([NEW_READ, ORDERISH]), _LLM())
    assert [r["tool_name"] for r in env.db.classifications] == ["account_cash_report"]
    assert env.reapplied == ["moomoo"]


@pytest.mark.asyncio
async def test_groupings_go_into_force_when_the_description_fails(env, monkeypatch):
    fake = FakeRunner({"account_cash_report": (Effect.READ, "account", 0.9)})
    fake.profile_error = RuntimeError("model down")
    _use_runner(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="model down"):
        await worker.run_job(_job([NEW_READ]), _LLM())
    assert [r["tool_name"] for r in env.db.classifications] == ["account_cash_report"]
    assert env.reapplied == ["moomoo"]


@pytest.mark.asyncio
async def test_a_users_server_is_described_on_their_model_and_never_grouped(env, monkeypatch):
    tool = {"name": "lookup", "description": "", "input_schema": {}}
    fake = FakeRunner({})
    _use_runner(monkeypatch, fake)
    await worker.run_job(
        _job([tool], scope="user:u1:mine", user_id="u1", server="mine"), _LLM()
    )
    assert env.db.profiles[0]["model"] == "user-model"
    assert env.db.profiles[0]["scope_key"] == "user:u1:mine"
    assert (fake.grouped, env.db.classifications) == ([], [])
    assert (env.bumped, env.reapplied) == (["u1"], [])
