"""Where an automation's turn runs: the row it names, routed as any turn's."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.home_workspace import get_home_workspace_id
from src.server.services.automation_executor import AutomationExecutor, _Place, _turn_place
from src.server.services.turn_runtime import turn_route

_USER = "user-1"
_WS = "11111111-1111-1111-1111-111111111111"
_THREAD = "22222222-2222-2222-2222-222222222222"
_HOME = get_home_workspace_id(_USER)
_PROJECT = {"workspace_id": _WS, "user_id": _USER, "status": "running"}
_HOME_ROW = {"workspace_id": _HOME, "user_id": _USER, "status": "unbound"}


def _automation(**overrides):
    return {
        "user_id": _USER,
        "workspace_id": _HOME,
        "thread_strategy": "continue",
        "conversation_thread_id": _THREAD,
        **overrides,
    }


async def _route(automation, *, thread_meta=None, workspace=None):
    with (
        patch(
            "src.server.database.conversation.get_thread_auth_meta",
            new=AsyncMock(return_value=thread_meta),
        ),
        patch(
            "src.server.database.workspace.get_workspace",
            new=AsyncMock(return_value=workspace),
        ),
    ):
        route = turn_route(_USER, (await _turn_place(automation)).workspace)
    return route.workspace_id, route.role


@pytest.mark.asyncio
async def test_continuing_a_workspace_thread_runs_that_workspaces_analyst():
    assert await _route(
        _automation(workspace_id=_WS),
        thread_meta={"user_id": _USER, "workspace_id": _WS},
        workspace=_PROJECT,
    ) == (_WS, "analyst")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "automation, thread_meta",
    [
        (_automation(), {"user_id": _USER, "workspace_id": _HOME}),
        (_automation(conversation_thread_id=None), None),
        (_automation(thread_strategy="new"), {"user_id": _USER, "workspace_id": _WS}),
    ],
    ids=["home-thread", "not-pinned-yet", "new-thread-each-run"],
)
async def test_any_other_firing_of_a_home_automation_runs_in_home(automation, thread_meta):
    assert await _route(
        automation, thread_meta=thread_meta, workspace=_HOME_ROW
    ) == (_HOME, "chief_of_staff")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace, expected",
    [(_PROJECT, (_WS, "analyst")), (_HOME_ROW, (_HOME, "chief_of_staff"))],
    ids=["workspace", "chief-of-staff-home"],
)
async def test_a_firing_runs_in_its_stored_workspace(workspace, expected):
    automation = _automation(workspace_id=workspace["workspace_id"])
    assert await _route(automation, workspace=workspace) == expected


@pytest.mark.asyncio
async def test_a_firing_whose_workspace_is_gone_fails_before_any_thread():
    with pytest.raises(ValueError, match="may have been deleted"):
        await _route(_automation(workspace_id=_WS), workspace=None)



# One rule for every row: its stored workspace, continuing the pin only there.

_OTHER_WS = "33333333-3333-3333-3333-333333333333"
_OTHER = {"workspace_id": _OTHER_WS, "user_id": _USER, "status": "running"}


async def _place_by_id(automation, *, thread_meta, rows):
    """As ``_route``, with each workspace read answered by its id, and the
    thread the run continues (None starts one)."""
    with (
        patch(
            "src.server.database.conversation.get_thread_auth_meta",
            new=AsyncMock(return_value=thread_meta),
        ),
        patch(
            "src.server.database.workspace.get_workspace",
            new=AsyncMock(side_effect=lambda ws_id: rows.get(ws_id)),
        ),
    ):
        place = await _turn_place(automation)
    route = turn_route(_USER, place.workspace)
    return route.workspace_id, route.role, place.thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [_WS, _HOME], ids=["workspace", "home"])
@pytest.mark.parametrize(
    "thread_meta",
    [
        {"user_id": _USER, "workspace_id": _OTHER_WS},
        {"user_id": _USER, "workspace_id": "44444444-4444-4444-4444-444444444444"},
        {"user_id": "someone-else", "workspace_id": _WS},
    ],
    ids=["another-workspace", "a-deleted-workspace", "another-account"],
)
async def test_a_pin_outside_the_stored_workspace_starts_a_thread_there(stored, thread_meta):
    """The automation runs where it lives; only a thread there is continued."""
    rows = {_WS: _PROJECT, _HOME: _HOME_ROW, _OTHER_WS: _OTHER}
    expected = (_WS, "analyst") if stored == _WS else (_HOME, "chief_of_staff")
    assert await _place_by_id(
        _automation(workspace_id=stored), thread_meta=thread_meta, rows=rows
    ) == (*expected, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "automation, expected",
    [
        (_automation(workspace_id=_WS), (_WS, "analyst")),
        (_automation(workspace_id=_HOME), (_HOME, "chief_of_staff")),
    ],
    ids=["workspace-row", "home-row"],
)
async def test_a_pin_in_the_stored_workspace_is_continued(automation, expected):
    thread_meta = {"user_id": _USER, "workspace_id": automation["workspace_id"]}
    assert await _place_by_id(
        automation, thread_meta=thread_meta, rows={_WS: _PROJECT, _HOME: _HOME_ROW}
    ) == (*expected, _THREAD)


@pytest.mark.asyncio
async def test_a_pinned_firing_whose_workspace_is_gone_still_fails():
    automation = _automation(workspace_id=_WS)
    with pytest.raises(ValueError, match="may have been deleted"):
        await _place_by_id(
            automation,
            thread_meta={"user_id": _USER, "workspace_id": _OTHER_WS},
            rows={_HOME: _HOME_ROW},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "automation, expected",
    [
        (_automation(workspace_id=_WS), (_WS, "analyst")),
        (_automation(workspace_id=_HOME), (_HOME, "chief_of_staff")),
    ],
    ids=["workspace-row", "home-row"],
)
async def test_a_pinned_thread_that_is_gone_is_made_again_where_the_row_lives(automation, expected):
    assert await _place_by_id(
        automation, thread_meta=None, rows={_WS: _PROJECT, _HOME: _HOME_ROW}
    ) == (*expected, _THREAD)


@pytest.mark.asyncio
async def test_a_run_that_leaves_its_pin_behind_pins_the_thread_it_starts():
    """So the next run continues the new thread, not the one it left."""
    executor = AutomationExecutor()
    automation = _automation(workspace_id=_WS, automation_id="auto-1")
    with (
        patch("src.server.services.automation_executor.auto_db") as auto_db,
        patch.object(
            executor, "_precreate_titled_thread", new=AsyncMock(return_value=True)
        ),
    ):
        auto_db.update_automation = AsyncMock()
        thread_id = await executor._resolve_thread(automation, _Place(_PROJECT, None), _WS)

    assert thread_id != _THREAD
    auto_db.update_automation.assert_awaited_once_with(
        "auto-1", _USER, conversation_thread_id=thread_id
    )


# ---------------------------------------------------------------------------
# Pruning an automation's own finished threads before it starts a new one.
# ---------------------------------------------------------------------------

from src.server.services import automation_executor as _ae  # noqa: E402
from unittest.mock import Mock  # noqa: E402


def _row(thread_id, status="completed", workspace_id=_WS):
    return {
        "conversation_thread_id": thread_id,
        "workspace_id": workspace_id,
        "current_status": status,
    }


def _new_automation(**overrides):
    automation = _automation(
        automation_id="33333333-3333-3333-3333-333333333333",
        thread_strategy="new",
        conversation_thread_id=None,
        metadata={"prune_previous_threads": True},
    )
    automation.update(overrides)
    return automation


def test_should_prune_only_for_a_new_automation_that_opted_in():
    assert _ae._should_prune_previous_threads(_new_automation()) is True
    assert _ae._should_prune_previous_threads(_automation(thread_strategy="new")) is False
    assert (
        _ae._should_prune_previous_threads(
            _new_automation(metadata={"prune_previous_threads": False})
        )
        is False
    )
    assert (
        _ae._should_prune_previous_threads(
            _new_automation(metadata=None)
        )
        is False
    )
    assert _ae._should_prune_previous_threads(_automation()) is False


@pytest.mark.asyncio
async def test_resolve_thread_prunes_only_when_new_and_opted_in():
    executor = AutomationExecutor()
    with (
        patch.object(
            executor, "_precreate_titled_thread", new=AsyncMock(return_value=True)
        ),
        patch.object(
            executor, "_prune_finished_threads", new=AsyncMock(return_value=0)
        ) as prune,
    ):
        await executor._resolve_thread(_new_automation(), _Place(_PROJECT, None), _WS)
        prune.assert_awaited_once()

    executor = AutomationExecutor()
    with (
        patch("src.server.services.automation_executor.auto_db") as auto_db,
        patch.object(
            executor, "_precreate_titled_thread", new=AsyncMock(return_value=True)
        ),
        patch.object(_ae, "_fetch_prune_rows", new=AsyncMock()) as fetch,
    ):
        auto_db.update_automation = AsyncMock()
        await executor._resolve_thread(
            _automation(workspace_id=_WS, automation_id="auto-1"),
            _Place(_PROJECT, None),
            _WS,
        )
        fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_deletes_every_finished_thread_including_a_single_one():
    executor = AutomationExecutor()
    rows = [_row("aaaaaaaa-0000-0000-0000-000000000001", "completed")]
    with (
        patch.object(_ae, "_fetch_prune_rows", new=AsyncMock(return_value=rows)),
        patch.object(_ae, "_delete_pruned_thread", new=AsyncMock()) as delete,
    ):
        count = await executor._prune_finished_threads(_new_automation(), "keep")

    assert count == 1
    delete.assert_awaited_once()
    assert delete.await_args.args[0] == "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_prune_skips_a_thread_that_is_not_finished_and_keeps_going():
    executor = AutomationExecutor()
    rows = [
        _row("aaaaaaaa-0000-0000-0000-000000000001", "in_progress"),
        _row("aaaaaaaa-0000-0000-0000-000000000002", "completed"),
    ]
    with (
        patch.object(_ae, "_fetch_prune_rows", new=AsyncMock(return_value=rows)),
        patch.object(_ae, "_delete_pruned_thread", new=AsyncMock()) as delete,
    ):
        count = await executor._prune_finished_threads(_new_automation(), "keep")

    assert count == 1
    delete.assert_awaited_once()
    assert delete.await_args.args[0] == "aaaaaaaa-0000-0000-0000-000000000002"


@pytest.mark.asyncio
async def test_prune_returns_zero_without_a_user_or_a_real_automation_id():
    executor = AutomationExecutor()
    with patch.object(_ae, "_fetch_prune_rows", new=AsyncMock()) as fetch:
        assert await executor._prune_finished_threads(
            _new_automation(user_id=None), "keep"
        ) == 0
        assert await executor._prune_finished_threads(
            _new_automation(automation_id="auto-1"), "keep"
        ) == 0
        fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_skips_a_thread_the_delete_refuses_and_still_deletes_the_next():
    from src.server.services.thread_mutation import MutationConflict

    executor = AutomationExecutor()
    rows = [
        _row("aaaaaaaa-0000-0000-0000-000000000001", "completed"),
        _row("aaaaaaaa-0000-0000-0000-000000000002", "completed"),
    ]
    delete = AsyncMock(
        side_effect=[MutationConflict("thread_busy", "delete", "busy"), None]
    )
    with (
        patch.object(_ae, "_fetch_prune_rows", new=AsyncMock(return_value=rows)),
        patch.object(_ae, "_delete_pruned_thread", new=delete),
    ):
        count = await executor._prune_finished_threads(_new_automation(), "keep")

    assert count == 1
    assert delete.await_count == 2


@pytest.mark.asyncio
async def test_delete_pruned_thread_guards_with_the_refusal_and_never_cancels():
    from src.server.services import automation_executor as _ae_local

    delete_mock = AsyncMock()
    refuse = Mock(return_value=None)
    cancel = AsyncMock()
    with (
        patch(
            "src.server.services.commands.control.delete_thread_ending_commands",
            new=delete_mock,
        ),
        patch(
            "src.server.database.order_attempts.refuse_attempts_on_thread_delete",
            new=refuse,
        ),
        patch(
            "src.server.services.thread_lifecycle_feed.publish_thread_deleted",
            new=AsyncMock(),
        ),
        patch(
            "src.server.services.workspace_manager.prune_thread_dirs_soon",
            new=Mock(),
        ),
        patch.object(_ae_local.tl_db, "request_run_cancel", new=cancel),
    ):
        await _ae_local._delete_pruned_thread("thread-9", _USER, _WS)

    assert delete_mock.await_args.args[0] == "thread-9"
    before_delete = delete_mock.await_args.kwargs["before_delete"]
    before_delete("CONN")
    refuse.assert_called_once_with("thread-9", conn="CONN")
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_thread_skips_prune_when_precreate_reports_failure():
    executor = AutomationExecutor()
    with (
        patch.object(
            executor, "_precreate_titled_thread", new=AsyncMock(return_value=False)
        ),
        patch.object(
            executor, "_prune_finished_threads", new=AsyncMock(return_value=0)
        ) as prune,
    ):
        thread_id = await executor._resolve_thread(
            _new_automation(), _Place(_PROJECT, None), _WS
        )

    assert thread_id
    prune.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_precreate_that_throws_is_reported_as_failure_and_never_prunes():
    executor = AutomationExecutor()
    with (
        patch(
            "src.server.database.conversation.create_thread",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(
            executor, "_prune_finished_threads", new=AsyncMock(return_value=0)
        ) as prune,
    ):
        thread_id = await executor._resolve_thread(
            _new_automation(name="Nightly"), _Place(_PROJECT, None), _WS
        )

    assert thread_id
    prune.assert_not_awaited()
