"""Serial-run guard and post-settle prune for 'new' prune_previous_threads.

A 'new' automation that opted into prune_previous_threads must not stack
same-name threads when a run outlasts the cron interval: an active earlier
firing makes the new one wait, and only one firing may be queued. When a
firing settles, the newest thread is kept and any older finished thread of
the same automation is removed.
"""

import asyncio as _asyncio
import datetime as _dt
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services import automation_executor as ex_mod
from src.server.services import automation_settlement as settlement_mod
from src.server.services.automation_executor import (
    AutomationExecutor,
    _active_prune_busy,
    _should_prune_previous_threads,
    prune_superseded_threads,
)

_AID = "8e67b587-5a32-4bcf-bdef-8f722defd7f2"
_USER = "user-1"
_THREAD = "22222222-2222-2222-2222-222222222222"


async def _fast_timeout(coro, timeout):
    """Stand-in for asyncio.wait_for that times out immediately.

    Closes the coroutine it was handed so the real ``Event.wait`` is not
    left un-awaited (avoids a RuntimeWarning in the poll tests).
    """
    if hasattr(coro, "close"):
        coro.close()
    raise _asyncio.TimeoutError


def _automation(**overrides):
    return {
        "automation_id": _AID,
        "user_id": _USER,
        "thread_strategy": "new",
        "metadata": {"prune_previous_threads": True},
        **overrides,
    }


def test_should_prune_only_for_opted_in_new():
    assert _should_prune_previous_threads(_automation()) is True
    assert _should_prune_previous_threads(
        _automation(thread_strategy="continue")
    ) is False
    assert _should_prune_previous_threads(_automation(metadata={})) is False
    assert _should_prune_previous_threads(_automation(metadata=None)) is False


@pytest.mark.asyncio
async def test_active_prune_busy_none_without_earlier_execution():
    with patch.object(
        ex_mod.exec_db, "get_active_execution", new=AsyncMock(return_value=None)
    ):
        assert await _active_prune_busy(_AID, "me") is None


@pytest.mark.asyncio
async def test_active_prune_busy_when_previous_row_running():
    row = {
        "automation_execution_id": "prev",
        "conversation_thread_id": None,
        "status": "running",
    }
    with patch.object(
        ex_mod.exec_db, "get_active_execution", new=AsyncMock(return_value=row)
    ):
        assert await _active_prune_busy(_AID, "me") == row


@pytest.mark.asyncio
async def test_active_prune_busy_uses_thread_liveness():
    row = {
        "automation_execution_id": "prev",
        "conversation_thread_id": _THREAD,
        "status": "completed",
    }
    with (
        patch.object(
            ex_mod.exec_db, "get_active_execution", new=AsyncMock(return_value=row)
        ),
        patch.object(ex_mod, "_thread_busy", new=AsyncMock(return_value=True)),
    ):
        assert await _active_prune_busy(_AID, "me") == row
    with (
        patch.object(
            ex_mod.exec_db, "get_active_execution", new=AsyncMock(return_value=row)
        ),
        patch.object(ex_mod, "_thread_busy", new=AsyncMock(return_value=False)),
    ):
        assert await _active_prune_busy(_AID, "me") is None


@pytest.mark.asyncio
async def test_wait_for_previous_execution_runs_once_free():
    executor = AutomationExecutor()
    fresh = {"automation_id": _AID, "user_id": _USER, "status": "active"}
    with (
        patch.object(
            ex_mod.exec_db,
            "get_execution_status",
            new=AsyncMock(return_value="waiting"),
        ),
        patch.object(
            ex_mod.exec_db,
            "try_admit_prune_execution",
            new=AsyncMock(return_value="admitted"),
        ),
        patch.object(
            ex_mod.auto_db, "get_automation", new=AsyncMock(return_value=fresh)
        ),
        patch.object(
            ex_mod, "_active_prune_busy", new=AsyncMock(return_value=None)
        ),
    ):
        got = await executor._wait_for_previous_execution(
            _automation(), "me", time.monotonic() + 60
        )
    assert got == fresh


@pytest.mark.asyncio
async def test_wait_for_previous_execution_skips_when_queued():
    executor = AutomationExecutor()
    settle_mock = AsyncMock()
    with (
        patch.object(
            ex_mod.exec_db,
            "get_execution_status",
            new=AsyncMock(return_value="waiting"),
        ),
        patch.object(
            ex_mod.exec_db,
            "try_admit_prune_execution",
            new=AsyncMock(return_value="skip_queued"),
        ),
        patch.object(ex_mod, "settle", new=settle_mock),
    ):
        got = await executor._wait_for_previous_execution(
            _automation(), "me", time.monotonic() + 60
        )
    assert got is None
    assert settle_mock.await_args.kwargs["skip_reason"] == "thread_busy"


@pytest.mark.asyncio
async def test_wait_for_previous_execution_skips_at_deadline_while_busy():
    executor = AutomationExecutor()
    settle_mock = AsyncMock()
    with (
        patch.object(
            ex_mod.exec_db,
            "get_execution_status",
            new=AsyncMock(return_value="waiting"),
        ),
        patch.object(
            ex_mod.exec_db,
            "try_admit_prune_execution",
            new=AsyncMock(return_value="wait"),
        ),
        patch.object(ex_mod, "settle", new=settle_mock),
    ):
        got = await executor._wait_for_previous_execution(
            _automation(), "me", time.monotonic() - 1
        )
    assert got is None
    assert settle_mock.await_args.kwargs["skip_reason"] == "thread_busy"


@pytest.mark.asyncio
async def test_prune_superseded_threads_deletes_older_than_latest():
    fake_executor = MagicMock()
    fake_executor._prune_finished_threads = AsyncMock(return_value=2)
    with (
        patch.object(
            ex_mod,
            "_latest_automation_thread_id",
            new=AsyncMock(return_value="latest"),
        ),
        patch.object(
            ex_mod.AutomationExecutor, "get_instance", return_value=fake_executor
        ),
    ):
        got = await prune_superseded_threads(_automation())
    assert got == ["latest"]
    fake_executor._prune_finished_threads.assert_awaited_once()
    assert fake_executor._prune_finished_threads.await_args.args[1] == "latest"


@pytest.mark.asyncio
async def test_prune_superseded_threads_noop_when_not_opted_in():
    with patch.object(
        ex_mod, "_latest_automation_thread_id", new=AsyncMock()
    ) as latest:
        assert (
            await prune_superseded_threads(_automation(thread_strategy="continue"))
            == []
        )
    latest.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_superseded_threads_swallows_errors():
    fake_executor = MagicMock()
    fake_executor._prune_finished_threads = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    with (
        patch.object(
            ex_mod,
            "_latest_automation_thread_id",
            new=AsyncMock(return_value="latest"),
        ),
        patch.object(
            ex_mod.AutomationExecutor, "get_instance", return_value=fake_executor
        ),
    ):
        assert await prune_superseded_threads(_automation()) == []


@pytest.mark.asyncio
async def test_after_settling_prunes_superseded_threads():
    prune_mock = AsyncMock()
    row = {"settled_from": "running"}
    with (
        patch.object(ex_mod, "prune_superseded_threads", new=prune_mock),
        patch.object(settlement_mod, "safe_add", new=MagicMock()),
    ):
        await settlement_mod._after_settling(
            settlement_mod.Outcome.SKIPPED,
            _automation(),
            "me",
            row,
            None,
            None,
            None,
            None,
            None,
            None,
            False,
        )
    prune_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_after_settling_survives_prune_failure():
    row = {"settled_from": "running"}
    with (
        patch.object(
            ex_mod,
            "prune_superseded_threads",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(settlement_mod, "safe_add", new=MagicMock()),
    ):
        # Must not raise into the settlement.
        await settlement_mod._after_settling(
            settlement_mod.Outcome.SKIPPED,
            _automation(),
            "me",
            row,
            None,
            None,
            None,
            None,
            None,
            None,
            False,
        )


# ──────────────────────────────────────────────────────────────────────
# Races found by review: strict-older prune anchor and atomic admission.
# These drive the real ``try_admit_prune_execution`` over an in-memory table
# guarded by one shared asyncio.Lock, so the two contenders see one another.
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_prune_sql_requires_strictly_older_than_keep():
    sql = ex_mod._PRUNE_THREADS_SQL
    assert "JOIN conversation_threads keep_ct" in sql
    assert "(ct.created_at, ct.conversation_thread_id)" in sql
    assert "< (keep_ct.created_at, keep_ct.conversation_thread_id)" in sql
    assert "conversation_thread_id::text <>" not in sql


_BASE = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)


def _row(aid, status, created, thread=None):
    return {
        "automation_execution_id": None,
        "automation_id": aid,
        "status": status,
        "conversation_thread_id": thread,
        "created_at": created,
        "started_at": None,
    }


class _FakeDB:
    def __init__(self, automations, executions):
        self.automations = automations
        self.executions = executions
        self.responses = []
        self.lock = _asyncio.Lock()


class _FakeTxn:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        await self.db.lock.acquire()
        return self

    async def __aexit__(self, *exc):
        self.db.lock.release()
        return False


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self._one = None
        self._many = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        self._one = None
        self._many = []
        s = " ".join(sql.split())
        if "FROM automations" in s and "FOR UPDATE" in s:
            aid = params[0]
            self._one = (
                {"automation_id": aid, "status": self.db.automations[aid]}
                if aid in self.db.automations
                else None
            )
        elif "FROM automation_executions WHERE automation_execution_id" in s:
            row = self.db.executions.get(params[0])
            self._one = dict(row) if row else None
        elif "FROM automation_executions e" in s:
            aid, me = params
            out = []
            for eid, row in self.db.executions.items():
                if eid == me or row["automation_id"] != aid:
                    continue
                thread_live = any(
                    tid == row["conversation_thread_id"] and st == "in_progress"
                    for tid, st in self.db.responses
                )
                if row["status"] not in ("running", "waiting") and not thread_live:
                    continue
                r = dict(row)
                r["thread_live"] = thread_live
                out.append(r)
            self._many = out
        elif (
            "RETURNING automation_execution_id" in s
            and "SET status = 'waiting'" in s
        ):
            eid, allowed = params[0], list(params[1])
            row = self.db.executions.get(eid)
            if row is None or row["status"] not in allowed:
                self._one = None
            else:
                row["status"] = "waiting"
                row["started_at"] = row.get("started_at") or _BASE
                self._one = {"automation_execution_id": eid}
        elif (
            "RETURNING automation_execution_id" in s
            and "SET status = 'running'" in s
        ):
            eid, allowed = params[0], list(params[1])
            row = self.db.executions.get(eid)
            if row is None or row["status"] not in allowed:
                self._one = None
            else:
                row["status"] = "running"
                row["started_at"] = row.get("started_at") or _BASE
                self._one = {"automation_execution_id": eid}
        else:
            raise AssertionError("unexpected SQL: " + s)

    async def fetchone(self):
        return self._one

    async def fetchall(self):
        return self._many


class _FakeConn:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self, row_factory=None):
        return _FakeCursor(self.db)

    def transaction(self):
        return _FakeTxn(self.db)


def _seed(execs):
    db = _FakeDB({_AID: "active"}, {})
    for eid, (status, created, thread) in execs.items():
        row = _row(_AID, status, created, thread)
        row["automation_execution_id"] = eid
        db.executions[eid] = row
    return db


async def _admit(db, eid, from_statuses=("pending",)):
    with patch.object(
        ex_mod.exec_db, "get_db_connection", new=lambda: _FakeConn(db)
    ):
        return await ex_mod.exec_db.try_admit_prune_execution(
            _AID, eid, from_statuses=from_statuses
        )


@pytest.mark.asyncio
async def test_admit_earlier_waiting_wins_over_newer_trigger():
    # X just finished, A is queued waiting, B arrives. B first must skip;
    # then A is admitted. Only A ever runs.
    db = _seed(
        {
            "X": ("completed", _BASE, "tx"),
            "A": ("waiting", _BASE + _dt.timedelta(minutes=1), None),
            "B": ("pending", _BASE + _dt.timedelta(minutes=2), None),
        }
    )
    assert await _admit(db, "B") == "skip_queued"
    assert await _admit(db, "A", from_statuses=("waiting",)) == "admitted"
    assert db.executions["A"]["status"] == "running"
    # B skips but its row is left waiting, the only state settle(SKIPPED) ends.
    assert db.executions["B"]["status"] == "waiting"


@pytest.mark.asyncio
async def test_admit_a_first_then_b_never_double_runs():
    # A grabs the lock first and is admitted; B then sees a live run and may
    # only queue — never a second running firing.
    db = _seed(
        {
            "X": ("completed", _BASE, "tx"),
            "A": ("waiting", _BASE + _dt.timedelta(minutes=1), None),
            "B": ("pending", _BASE + _dt.timedelta(minutes=2), None),
        }
    )
    assert await _admit(db, "A", from_statuses=("waiting",)) == "admitted"
    second = await _admit(db, "B")
    assert second in ("wait", "skip_queued")
    running = [
        eid for eid, row in db.executions.items() if row["status"] == "running"
    ]
    assert running == ["A"]


@pytest.mark.asyncio
async def test_admit_two_fresh_triggers_one_runs_one_waits():
    db = _seed(
        {
            "A": ("pending", _BASE, None),
            "B": ("pending", _BASE + _dt.timedelta(seconds=1), None),
        }
    )
    first = await _admit(db, "A")
    second = await _admit(db, "B")
    assert first == "admitted"
    assert second == "wait"
    assert db.executions["A"]["status"] == "running"
    assert db.executions["B"]["status"] == "waiting"


@pytest.mark.asyncio
async def test_admit_gone_when_execution_or_automation_missing():
    db = _seed({"A": ("pending", _BASE, None)})
    assert await _admit(db, "missing") == "gone"
    db2 = _FakeDB({}, {})
    with patch.object(
        ex_mod.exec_db, "get_db_connection", new=lambda: _FakeConn(db2)
    ):
        assert (
            await ex_mod.exec_db.try_admit_prune_execution(_AID, "A") == "gone"
        )


@pytest.mark.asyncio
async def test_admit_completed_execution_with_live_thread_blocks():
    # X already settled to 'completed' but its thread still has an
    # in_progress response; a later A must wait, not run on top of it.
    db = _FakeDB({_AID: "active"}, {})
    x = _row(_AID, "completed", _BASE, "tx")
    x["automation_execution_id"] = "X"
    db.executions["X"] = x
    a = _row(_AID, "pending", _BASE + _dt.timedelta(minutes=1), None)
    a["automation_execution_id"] = "A"
    db.executions["A"] = a
    db.responses = [("tx", "in_progress")]
    assert await _admit(db, "A") == "wait"
    assert db.executions["A"]["status"] == "waiting"


class _MemThreads:
    def __init__(self, rows):
        self.rows = list(rows)

    def ids(self):
        return sorted(r["id"] for r in self.rows)


def _mem_latest(table):
    async def _latest(automation):
        if not table.rows:
            return None
        row = max(table.rows, key=lambda r: (r["created_at"], r["id"]))
        return row["id"]

    return _latest


def _mem_fetch(table):
    async def _fetch(automation_uuid, user_id, keep_thread_id):
        keep = next((r for r in table.rows if r["id"] == keep_thread_id), None)
        if keep is None:
            # keep_ct JOIN with a missing row yields zero candidates.
            return []
        key = (keep["created_at"], keep["id"])
        older = [
            r
            for r in table.rows
            if (r["created_at"], r["id"]) < key
            and r["status"] in ("completed", "error", "cancelled")
        ]
        older.sort(key=lambda r: (r["created_at"], r["id"]))
        return [
            {
                "conversation_thread_id": r["id"],
                "workspace_id": "ws",
                "current_status": r["status"],
            }
            for r in older
        ]

    return _fetch


def _mem_delete(table):
    async def _delete(thread_id, user_id, workspace_id):
        table.rows = [r for r in table.rows if r["id"] != thread_id]

    return _delete


def _thr(tid, minutes):
    return {
        "id": tid,
        "created_at": _BASE + _dt.timedelta(minutes=minutes),
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_interleaved_settlement_prunes_preserve_newer_thread(monkeypatch):
    # Prune A picks keep=T1, pauses before fetching. T2 lands and completes,
    # prune B keeps T2 and removes T0/T1. A resumes: its stale keep T1 is gone,
    # so the keep_ct JOIN yields nothing and T2 must survive.
    table = _MemThreads([_thr("T0", 0), _thr("T1", 1)])
    reached = _asyncio.Event()
    released = _asyncio.Event()
    paused = {"done": False}
    base_fetch = _mem_fetch(table)

    async def _fetch_that_pauses(automation_uuid, user_id, keep_thread_id):
        if not paused["done"] and keep_thread_id == "T1":
            paused["done"] = True
            reached.set()
            await released.wait()
        return await base_fetch(automation_uuid, user_id, keep_thread_id)

    monkeypatch.setattr(ex_mod, "_latest_automation_thread_id", _mem_latest(table))
    monkeypatch.setattr(ex_mod, "_fetch_prune_rows", _fetch_that_pauses)
    monkeypatch.setattr(ex_mod, "_delete_pruned_thread", _mem_delete(table))

    auto = _automation()
    prune_a = _asyncio.create_task(prune_superseded_threads(auto))
    await reached.wait()

    table.rows.append(_thr("T2", 2))
    await prune_superseded_threads(auto)  # prune B: keep T2, delete T0, T1
    assert table.ids() == ["T2"]

    released.set()
    await prune_a
    assert table.ids() == ["T2"]


@pytest.mark.asyncio
async def test_stale_keep_never_selects_a_newer_thread(monkeypatch):
    # keep=T1 still exists, but T2 was created later: T2 is never a candidate.
    table = _MemThreads([_thr("T0", 0), _thr("T1", 1), _thr("T2", 2)])
    monkeypatch.setattr(ex_mod, "_latest_automation_thread_id", _mem_latest(table))
    monkeypatch.setattr(ex_mod, "_fetch_prune_rows", _mem_fetch(table))
    monkeypatch.setattr(ex_mod, "_delete_pruned_thread", _mem_delete(table))

    deleted = await AutomationExecutor.get_instance()._prune_finished_threads(
        _automation(), "T1"
    )
    assert deleted == 1
    assert table.ids() == ["T1", "T2"]


@pytest.mark.asyncio
async def test_admit_same_execution_concurrent_calls_admit_only_once():
    # The same pending execution fired twice at once must be claimed once.
    db = _seed({"A": ("pending", _BASE, None)})
    results = await _asyncio.gather(_admit(db, "A"), _admit(db, "A"))
    assert sorted(results) == ["admitted", "already_claimed"]
    assert db.executions["A"]["status"] == "running"
    # And a later repeat stays already-claimed.
    assert await _admit(db, "A") == "already_claimed"


@pytest.mark.asyncio
async def test_execute_returns_immediately_when_already_claimed():
    executor = AutomationExecutor()
    seen = {}

    async def fake_admit(automation_id, execution_id, *, from_statuses=("pending",)):
        seen["from"] = tuple(from_statuses)
        return "already_claimed"

    keep_alive = AsyncMock()
    resolve = AsyncMock()
    with (
        patch.object(ex_mod.exec_db, "try_admit_prune_execution", new=fake_admit),
        patch.object(ex_mod, "_keep_alive", new=keep_alive),
        patch.object(executor, "_resolve_thread", new=resolve),
    ):
        await executor.execute(_automation(), "A")
    assert seen["from"] == ("pending",)
    keep_alive.assert_not_awaited()
    resolve.assert_not_awaited()

@pytest.mark.asyncio
async def test_wait_admitted_recovers_after_transient_lookup_error_and_starts_once():
    executor = AutomationExecutor()
    fresh = {"automation_id": _AID, "user_id": _USER, "status": "active"}
    settle_mock = AsyncMock()
    transition = AsyncMock(return_value=True)
    with (
        patch.object(
            ex_mod.exec_db, "get_execution_status",
            new=AsyncMock(return_value="waiting"),
        ),
        patch.object(
            ex_mod.exec_db, "try_admit_prune_execution",
            new=AsyncMock(side_effect=["admitted", "admitted"]),
        ),
        patch.object(ex_mod.exec_db, "transition_execution", new=transition),
        patch.object(
            ex_mod.auto_db, "get_automation",
            new=AsyncMock(
                side_effect=[RuntimeError("transient db blip"), fresh]
            ),
        ),
        patch.object(ex_mod, "_active_prune_busy", new=AsyncMock(return_value=None)),
        patch.object(ex_mod, "settle", new=settle_mock),
        patch.object(
            ex_mod.asyncio, "wait_for",
            new=_fast_timeout,
        ),
    ):
        got = await executor._wait_for_previous_execution(
            _automation(), "me", time.monotonic() + 60
        )
    assert got == fresh
    assert settle_mock.await_count == 0
    assert transition.await_count >= 1


@pytest.mark.asyncio
async def test_wait_admitted_recovers_even_when_rollback_also_fails_once():
    executor = AutomationExecutor()
    fresh = {"automation_id": _AID, "user_id": _USER, "status": "active"}
    settle_mock = AsyncMock()
    transition = AsyncMock(side_effect=[RuntimeError("db blip"), True])
    with (
        patch.object(
            ex_mod.exec_db, "get_execution_status",
            new=AsyncMock(return_value="waiting"),
        ),
        patch.object(
            ex_mod.exec_db, "try_admit_prune_execution",
            new=AsyncMock(side_effect=["admitted", "admitted"]),
        ),
        patch.object(ex_mod.exec_db, "transition_execution", new=transition),
        patch.object(
            ex_mod.auto_db, "get_automation",
            new=AsyncMock(
                side_effect=[RuntimeError("transient db blip"), fresh]
            ),
        ),
        patch.object(ex_mod, "_active_prune_busy", new=AsyncMock(return_value=None)),
        patch.object(ex_mod, "settle", new=settle_mock),
        patch.object(
            ex_mod.asyncio, "wait_for",
            new=_fast_timeout,
        ),
    ):
        got = await executor._wait_for_previous_execution(
            _automation(), "me", time.monotonic() + 60
        )
    assert got == fresh
    assert settle_mock.await_count == 0


@pytest.mark.asyncio
async def test_wait_exits_without_settling_on_foreign_already_claimed():
    executor = AutomationExecutor()
    settle_mock = AsyncMock()
    with (
        patch.object(
            ex_mod.exec_db, "get_execution_status",
            new=AsyncMock(return_value="waiting"),
        ),
        patch.object(
            ex_mod.exec_db, "try_admit_prune_execution",
            new=AsyncMock(return_value="already_claimed"),
        ),
        patch.object(ex_mod, "settle", new=settle_mock),
    ):
        got = await executor._wait_for_previous_execution(
            _automation(), "me", time.monotonic() - 1
        )
    assert got is None
    assert settle_mock.await_count == 0


@pytest.mark.asyncio
async def test_execute_skip_queued_transitions_pending_to_waiting_so_settle_succeeds():
    db = _seed(
        {
            "W": ("waiting", _BASE, None),
            "B": ("pending", _BASE + _dt.timedelta(minutes=1), None),
        }
    )
    result = await _admit(db, "B", from_statuses=("pending",))
    assert result == "skip_queued"
    assert db.executions["B"]["status"] == "waiting"


@pytest.mark.asyncio
async def test_wait_recovers_after_transient_initial_status_read_error_and_starts_once():
    executor = AutomationExecutor()
    fresh = {"automation_id": _AID, "user_id": _USER, "status": "active"}
    settle_mock = AsyncMock()
    admit = AsyncMock(return_value="admitted")
    status = AsyncMock(
        side_effect=[RuntimeError("transient db error"), "waiting"]
    )
    with (
        patch.object(ex_mod.exec_db, "get_execution_status", new=status),
        patch.object(ex_mod.exec_db, "try_admit_prune_execution", new=admit),
        patch.object(
            ex_mod.exec_db, "transition_execution",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            ex_mod.auto_db, "get_automation",
            new=AsyncMock(return_value=fresh),
        ),
        patch.object(ex_mod, "_active_prune_busy", new=AsyncMock(return_value=None)),
        patch.object(ex_mod, "settle", new=settle_mock),
        patch.object(ex_mod.asyncio, "wait_for", new=_fast_timeout),
    ):
        got = await executor._wait_for_previous_execution(
            _automation(), "me", time.monotonic() + 60
        )
    assert got == fresh
    assert settle_mock.await_count == 0
    # Initial status read failed once, then recovered; admitted exactly once.
    assert status.await_count == 2
    assert admit.await_count == 1


@pytest.mark.asyncio
async def test_wait_admitted_recovers_and_releases_once_on_transient_rollback_error():
    # Post-admit self-heal: iteration 1 try_admit -> "admitted", get_automation
    # raises a transient error, the except-branch _release_claim rollback ALSO
    # fails (claimed_by_self stays True, DB row still "running"). Iteration 2
    # must re-admit immediately via from_st=("waiting","running") WITHOUT doing
    # a second loop-top release.
    executor = AutomationExecutor()
    fresh = {"automation_id": _AID, "user_id": _USER, "status": "active"}
    settle_mock = AsyncMock()
    admit = AsyncMock(side_effect=["admitted", "admitted"])
    transition = AsyncMock(side_effect=[RuntimeError("db blip"), True])
    with (
        patch.object(
            ex_mod.exec_db, "get_execution_status",
            new=AsyncMock(return_value="waiting"),
        ),
        patch.object(ex_mod.exec_db, "try_admit_prune_execution", new=admit),
        patch.object(ex_mod.exec_db, "transition_execution", new=transition),
        patch.object(
            ex_mod.auto_db, "get_automation",
            new=AsyncMock(side_effect=[RuntimeError("transient db blip"), fresh]),
        ),
        patch.object(ex_mod, "_active_prune_busy", new=AsyncMock(return_value=None)),
        patch.object(ex_mod, "settle", new=settle_mock),
        patch.object(ex_mod.asyncio, "wait_for", new=_fast_timeout),
    ):
        got = await executor._wait_for_previous_execution(
            _automation(), "me", time.monotonic() + 60
        )
    assert got == fresh
    assert settle_mock.await_count == 0
    # Rollback attempted exactly once (iteration 2 must not release again), and
    # iteration 2 re-admits directly -> two try_admit calls total.
    assert transition.await_count == 1
    assert admit.await_count == 2
