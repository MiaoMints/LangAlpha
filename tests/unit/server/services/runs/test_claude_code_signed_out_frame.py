"""A Claude Code token refused after admission reads as the admission refusal, not as this service failing."""

from src.llms.claude_code import ClaudeTurnFailed
from src.server.services.llm.claude_code import CLAUDE_CODE_OFF
from src.server.services.runs.stream_errors import error_event_data, failure_kind_metadata


def _raised(inner: BaseException) -> BaseException:
    try:
        try:
            raise inner
        except BaseException as e:
            raise RuntimeError("graph step failed") from e
    except RuntimeError as wrapped:
        return wrapped


def test_a_refused_token_is_framed_with_the_signed_out_refusal_and_no_kind():
    exc = _raised(ClaudeTurnFailed("auth_failed", "401 invalid token"))
    frame = error_event_data("t-1", str(exc), exc=exc, model="claude-haiku-5-5-code")
    assert frame == {
        "thread_id": "t-1", "error": CLAUDE_CODE_OFF["signed_out"], "message": CLAUDE_CODE_OFF["signed_out"],
        "type": "claude_code_off", "model": "claude-haiku-5-5-code", "reason": "signed_out",
    }
    assert failure_kind_metadata(exc) == {}


def test_any_other_claude_code_failure_keeps_its_classification():
    exc = _raised(ClaudeTurnFailed("api_error", "overloaded"))
    frame = error_event_data("t-1", str(exc), exc=exc)
    assert frame["error_kind"] == "internal" and frame["message"] == "An error occurred during processing"
    assert failure_kind_metadata(exc) == {"error_kind": "internal"}
