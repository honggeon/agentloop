"""Unified TestCase normalization, legacy mapping, and path safety validators."""

import pytest

from auditrail.testcases import (
    is_legacy_case,
    normalize_test_case,
    resolve_within,
    validate_case_relpath,
)


def test_normalize_unified_http_session_case():
    case = normalize_test_case(
        {
            "case_id": "tool-recovery-001",
            "version": 1,
            "title": "工具失败后恢复",
            "adapter": "http_session",
            "input": {"user_id": "u1", "message": "读取一个不存在的文件并说明如何恢复"},
            "expectations": [
                {"id": "e1", "type": "tool_error_is_observed", "required": True},
                {"id": "e2", "type": "final_answer_present"},
            ],
            "timeout_seconds": 120,
        }
    )
    assert case["case_id"] == "tool-recovery-001"
    assert case["adapter"] == "http_session"
    assert case["legacy"] is False
    assert case["expectations"][1]["required"] is True  # default


def test_normalize_requires_http_input_message_or_session():
    with pytest.raises(ValueError):
        normalize_test_case(
            {
                "case_id": "c1",
                "title": "t",
                "adapter": "http_session",
                "input": {"user_id": "u1"},
            }
        )


def test_normalize_rejects_unknown_adapter():
    with pytest.raises(ValueError):
        normalize_test_case({"case_id": "c1", "title": "t", "adapter": "grpc", "input": {}})


def test_legacy_case_maps_to_command_adapter():
    raw = {
        "id": "DSH-CLI-004",
        "title": "输出校验",
        "args": ["--version"],
        "files": {"home/config.yml": "a: 1"},
        "env": {"DSH_HOME": "{tmp}/home"},
        "expect": {"exit_code": 0},
        "xfail": True,
        "defect": "DSH-123",
        "session": {"recorded": True},
    }
    assert is_legacy_case(raw)
    case = normalize_test_case(raw)
    assert case["adapter"] == "command"
    assert case["legacy"] is True
    assert case["case_id"] == "DSH-CLI-004"
    assert case["input"]["args"] == ["--version"]
    assert case["input"]["env"] == {"DSH_HOME": "{tmp}/home"}
    assert case["fixtures"] == [{"path": "home/config.yml", "content": "a: 1"}]
    assert case["expectations"][0]["assertions"] == {"exit_code": 0}
    # xfail/defect/session semantics are preserved, not reinterpreted
    assert case["xfail"] is True
    assert case["defect"] == "DSH-123"
    assert case["adapter_config"]["session"] == {"recorded": True}


def test_validate_case_relpath_rejects_unsafe_paths():
    for bad in ("", "/abs/path", "../escape", "a/../../b", "a\\b", "C:\\tmp\\x"):
        with pytest.raises(ValueError):
            validate_case_relpath(bad)
    with pytest.raises(ValueError):
        validate_case_relpath("a\x00b")
    assert validate_case_relpath("home/config.yml") == "home/config.yml"


def test_resolve_within_rejects_escape_and_symlink(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    assert resolve_within(root, "sub/file.txt") == (root / "sub/file.txt").resolve()
    with pytest.raises(ValueError):
        resolve_within(root, "../outside.txt")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_within(root, "link")
    inside_dir = tmp_path / "elsewhere"
    inside_dir.mkdir()
    (root / "dirlink").symlink_to(inside_dir, target_is_directory=True)
    with pytest.raises(ValueError):
        resolve_within(root, "dirlink/file.txt")
