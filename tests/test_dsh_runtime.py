from pathlib import Path

import pytest

from auditrail.dsh_runtime import (
    DshExecutionResult,
    DshRunRequest,
    DshRuntimeConfig,
    DshRuntimeError,
    DshValidationError,
    validate_request,
    validate_session_id,
)


SESSION_ID = "session-12345678-1234-1234-1234-123456789abc"


def make_config(tmp_path: Path, **overrides) -> tuple[DshRuntimeConfig, Path]:
    root = tmp_path / "allowed"
    workspace = root / "project"
    workspace.mkdir(parents=True)
    values = {
        "binary": Path("/opt/dsh/bin/dsh"),
        "allowed_profiles": ("headless",),
        "allowed_workspace_roots": (root,),
        "default_timeout_seconds": 30,
        "max_timeout_seconds": 60,
    }
    values.update(overrides)
    return DshRuntimeConfig(**values), workspace


def test_runtime_config_validates_request(tmp_path):
    config, workspace = make_config(tmp_path)
    invocation = validate_request(
        config,
        DshRunRequest("run tests", "headless", workspace, SESSION_ID, 45),
    )

    assert invocation.workspace == workspace.resolve()
    assert invocation.session_id == SESSION_ID
    assert invocation.timeout_seconds == 45


@pytest.mark.parametrize("profile", ["other", "--patch", "headless/../../bad", ""])
def test_runtime_config_rejects_invalid_or_disallowed_profile(tmp_path, profile):
    config, workspace = make_config(tmp_path)
    with pytest.raises(DshValidationError, match="profile"):
        validate_request(config, DshRunRequest("hello", profile, workspace))


def test_runtime_config_rejects_workspace_root_outside_and_symlink_escape(tmp_path):
    config, workspace = make_config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = workspace.parent / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    for candidate in (workspace.parent, outside, escape, Path("relative")):
        with pytest.raises(DshValidationError, match="workspace"):
            validate_request(config, DshRunRequest("hello", "headless", candidate))


@pytest.mark.parametrize(
    "session_id",
    ["../session-id", "folder/session-id", r"folder\session-id", "--json", "", "   ", "bad\x00id"],
)
def test_validate_session_id_rejects_unsafe_values(session_id):
    with pytest.raises(DshValidationError, match="session"):
        validate_session_id(session_id)


def test_validate_session_id_preserves_opaque_identity():
    assert validate_session_id("legacy:custom_session.v2") == "legacy:custom_session.v2"


def test_runtime_config_rejects_relative_binary_and_empty_allowlists(tmp_path):
    with pytest.raises(DshValidationError, match="binary"):
        DshRuntimeConfig(Path("dsh"), ("headless",), (tmp_path,))
    with pytest.raises(DshValidationError, match="profile"):
        DshRuntimeConfig(Path("/opt/dsh"), (), (tmp_path,))
    with pytest.raises(DshValidationError, match="workspace"):
        DshRuntimeConfig(Path("/opt/dsh"), ("headless",), ())


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1, 61])
def test_runtime_config_rejects_non_finite_or_out_of_range_timeout(tmp_path, timeout):
    config, workspace = make_config(tmp_path)
    with pytest.raises(DshValidationError, match="timeout"):
        validate_request(config, DshRunRequest("hello", "headless", workspace, timeout_seconds=timeout))


def test_execution_result_success_requires_all_terminal_signals():
    base = dict(
        session_id=SESSION_ID,
        final_text="done",
        turn_end_reason="completed",
        exit_code=0,
        timed_out=False,
        cancelled=False,
        events=(),
    )
    assert DshExecutionResult(**base).succeeded is True
    assert DshExecutionResult(**base).events_are_provisional is True
    assert DshExecutionResult(**{**base, "session_id": None}).succeeded is False
    assert DshExecutionResult(**{**base, "exit_code": 1}).succeeded is False
    assert DshExecutionResult(**{**base, "turn_end_reason": "error"}).succeeded is False
    assert DshExecutionResult(**{**base, "final_text": None}).succeeded is False
    assert DshExecutionResult(**{**base, "error": DshRuntimeError("failure", "failed")}).succeeded is False
