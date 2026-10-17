"""Phase 3 promptfoo adapter: static import, safe export, no execution."""

import copy
import json

import pytest

from auditrail.integrations import promptfoo
from auditrail.integrations.external_models import MAX_COMMENT_LENGTH


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _config(**overrides):
    config = {
        "providers": [{"id": "openai:gpt-4o-mini", "label": "declared only"}],
        "tests": [
            {
                "description": "greets the user",
                "vars": {"name": "ada"},
                "assert": [
                    {"type": "icontains", "value": "hello"},
                    {"type": "regex", "value": "^hi"},
                ],
            }
        ],
    }
    config.update(overrides)
    return config


# ---------------------------------------------------------------------------
# JSON import
# ---------------------------------------------------------------------------


def test_import_json_config_success(tmp_path):
    report = promptfoo.import_config(tmp_path, _config())
    assert report["status"] == "imported"
    assert report["total_records"] == 1
    assert report["imported_records"] == 1
    assert report["invalid_records"] == 0
    drafts = promptfoo.load_drafts(tmp_path)
    assert len(drafts) == 1
    draft = drafts[0]
    # provider declaration is stored as data only
    assert draft["provider_declaration"]["id"] == "openai:gpt-4o-mini"
    assert draft["provider_declaration"]["executable"] is False
    # safe assertions mapped to static_* kinds
    kinds = [item["kind"] for item in draft["expectations"]]
    assert kinds == ["static_icontains", "static_regex"]
    assert all(item["safe_to_execute"] for item in draft["expectations"])
    assert draft["draft"] is True
    assert draft["safe_to_execute"] is True
    assert draft["vars"] == {"name": "ada"}


def test_import_is_idempotent_per_case(tmp_path):
    first = promptfoo.import_config(tmp_path, _config())
    second = promptfoo.import_config(tmp_path, _config())
    assert first["imported_records"] == 1
    assert second["duplicate_records"] == 1
    assert len(promptfoo.load_drafts(tmp_path)) == 1


def test_parse_config_text_invalid_json_raises_valueerror():
    with pytest.raises(ValueError, match="not valid JSON"):
        promptfoo.parse_config_text("{ this is not json")
    with pytest.raises(ValueError):
        promptfoo.parse_config_text("[1, 2, 3]")  # top level must be a mapping
    with pytest.raises(ValueError):
        promptfoo.parse_config_text("   ")


def test_parse_config_text_yaml_branch():
    yaml_text = "providers:\n  - openai:gpt-4o-mini\ntests:\n  - description: hi\n"
    try:
        import yaml  # noqa: F401
    except ImportError:
        # no PyYAML: YAML input is rejected explicitly, never parsed unsafely
        with pytest.raises(promptfoo.PromptfooDependencyError):
            promptfoo.parse_config_text(yaml_text)
        assert issubclass(promptfoo.PromptfooDependencyError, ValueError)
    else:
        payload = promptfoo.parse_config_text(yaml_text)
        assert payload["providers"] == ["openai:gpt-4o-mini"]
        with pytest.raises(ValueError, match="not valid YAML"):
            promptfoo.parse_config_text("providers: [unclosed\n  - broken: {")


# ---------------------------------------------------------------------------
# executable content is never executed / rejected
# ---------------------------------------------------------------------------


def test_provider_with_command_skipped_with_warning(tmp_path):
    marker = tmp_path / "pwned"
    config = _config(
        providers=[
            {"id": "evil", "command": "touch %s" % marker},
            {"id": "plain-provider"},
        ]
    )
    report = promptfoo.import_config(tmp_path, config)
    assert not marker.exists()  # nothing was executed
    drafts = promptfoo.load_drafts(tmp_path)
    assert len(drafts) == 1
    # the executable provider was skipped; the safe one became the declaration
    assert drafts[0]["provider_declaration"]["id"] == "plain-provider"
    assert any("executable content" in warning for warning in report["warnings"])


def test_test_entry_with_executable_keys_dropped_with_warning(tmp_path):
    marker = tmp_path / "pwned"
    config = _config(
        tests=[
            {
                "description": "has shell content",
                "command": "touch %s" % marker,
                "assert": [{"type": "equals", "value": "x"}],
            }
        ]
    )
    report = promptfoo.import_config(tmp_path, config)
    assert not marker.exists()
    assert report["imported_records"] == 1
    assert any("executable content" in warning for warning in report["warnings"])
    draft = promptfoo.load_drafts(tmp_path)[0]
    assert "command" not in draft


def test_unsafe_assertions_mapped_to_unmapped_not_dropped(tmp_path):
    config = _config(
        tests=[
            {
                "description": "uses llm rubric",
                "assert": [
                    {"type": "llm-rubric", "value": "is polite"},
                    {"type": "javascript", "value": "output.length > 0"},
                    {"type": "icontains", "value": "ok"},
                ],
            }
        ]
    )
    report = promptfoo.import_config(tmp_path, config)
    draft = promptfoo.load_drafts(tmp_path)[0]
    expectations = draft["expectations"]
    assert len(expectations) == 3  # nothing silently dropped
    by_type = {item["original_type"]: item for item in expectations}
    assert by_type["llm-rubric"]["kind"] == "unmapped"
    assert by_type["llm-rubric"]["safe_to_execute"] is False
    assert by_type["javascript"]["kind"] == "unmapped"
    assert by_type["javascript"]["safe_to_execute"] is False
    assert by_type["icontains"]["kind"] == "static_icontains"
    assert draft["unmapped_count"] == 2
    assert any("unmapped" in warning for warning in report["warnings"])


def test_provider_is_declaration_only(tmp_path):
    promptfoo.import_config(tmp_path, _config(providers=["openai:gpt-4o-mini"]))
    declaration = promptfoo.load_drafts(tmp_path)[0]["provider_declaration"]
    assert declaration == {"id": "openai:gpt-4o-mini", "executable": False}


def test_single_bad_test_entry_yields_partial(tmp_path):
    config = _config(tests=["not-an-object", {"description": "ok", "assert": []}])
    report = promptfoo.import_config(tmp_path, config)
    assert report["status"] == "partial"
    assert report["total_records"] == 2
    assert report["imported_records"] == 1
    assert report["invalid_records"] == 1
    assert len(promptfoo.load_drafts(tmp_path)) == 1


def test_draft_never_enters_production(tmp_path):
    config = _config(
        tests=[
            {
                "description": "draft with unmapped",
                "assert": [{"type": "llm-rubric", "value": "polite"}],
            }
        ]
    )
    report = promptfoo.import_config(tmp_path, config)
    draft = promptfoo.load_drafts(tmp_path)[0]
    assert draft["draft"] is True
    assert draft["safe_to_execute"] is False
    assert draft["source_system"] == "promptfoo"
    # unsafe drafts are counted as skipped, never as production-ready cases
    assert report["skipped_records"] == 1


# ---------------------------------------------------------------------------
# safe export
# ---------------------------------------------------------------------------


def _case(**overrides):
    case = {
        "case_id": "case-001",
        "version": 2,
        "title": "问候用例",
        "adapter": "http_session",
        "input": {
            "user_id": "user-1",
            "session_id": "sess-1",
            "message": "m" * (MAX_COMMENT_LENGTH + 200),
        },
        "expectations": [
            {"id": "exp-1", "type": "trajectory_contains", "required": True},
        ],
        "adapter_config": {
            "provider_id": "xuanjian-agent",
            "token": "super-secret-token-123",
            "authorization": "Bearer super-secret-token-123",
        },
    }
    case.update(overrides)
    return case


def test_export_case_is_safe_and_static():
    case = _case()
    before = copy.deepcopy(case)
    exported = promptfoo.export_case(case)
    text = json.dumps(exported, ensure_ascii=False)
    # no secrets anywhere in the export
    assert "super-secret-token-123" not in text
    assert "Bearer" not in text
    # no full private prompt: only a bounded excerpt
    assert "m" * (MAX_COMMENT_LENGTH + 200) not in text
    assert len(exported["tests"][0]["vars"]["message_excerpt"]) == MAX_COMMENT_LENGTH
    # the input case is not modified
    assert case == before
    # platform provenance markers are present
    assert exported["metadata"]["platform_schema_version"] == 1
    assert exported["metadata"]["platform"] == "auditrail"
    assert exported["metadata"]["case_id"] == "case-001"
    assert exported["promptfoo_compatibility"] == promptfoo.PROMPTFOO_COMPAT_VERSION
    # provider is a declaration only
    assert exported["providers"] == [{"id": "xuanjian-agent", "label": "declaration-only"}]
    # assertions are static references, never executable content
    assertion = exported["tests"][0]["assert"][0]
    assert assertion["type"] == "auditrail:trajectory_contains"
    assert assertion["id"] == "exp-1"
    assert assertion["required"] is True


def test_export_case_requires_case_id():
    with pytest.raises(ValueError):
        promptfoo.export_case({"title": "no id"})
    with pytest.raises(ValueError):
        promptfoo.export_case("not-a-dict")


def test_export_case_minimal_input():
    exported = promptfoo.export_case({"case_id": "c-1", "title": "t", "input": {}})
    assert exported["tests"][0]["vars"] == {}
    assert exported["tests"][0]["assert"] == []
