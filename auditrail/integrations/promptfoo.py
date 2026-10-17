"""promptfoo static asset import and safe export adapter (Phase 3).

This adapter handles **static promptfoo configuration assets only**. It never
executes anything:

- no promptfoo CLI invocation (the CLI is not required and never called);
- no provider execution — a provider is stored as a plain declaration;
- no ``command`` / ``plugin`` / shell / Python / JavaScript / template
  evaluation — such content is rejected or preserved as an ``unmapped``
  assertion with ``safe_to_execute: false``;
- the import never calls a real Agent and never creates runs.

YAML parsing uses ``yaml.safe_load`` when PyYAML is installed. When it is
not installed the adapter raises a clear error — it never falls back to an
unsafe parser. JSON promptfoo configs always work.

Import produces *draft* TestCase payloads (``draft: true``) stored under
``platform/external/promptfoo_drafts.json``. Drafts never enter a production
SuiteJob; promoting one to a real platform TestCase requires an explicit,
separate confirmation step (not part of this adapter).

Export (:func:`export_case`) renders one platform TestCase as a safe static
promptfoo JSON config: no endpoint secrets, no tokens/cookies/Authorization
headers, no full private prompts (long strings are truncated), and the
payload is data only — exporting never executes anything and never modifies
the TestCase.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..costs import redact_metadata, resolve_import_path
from ..run_contract import utc_now
from . import external_models as ext

SOURCE_SYSTEM = "promptfoo"
PROMPTFOO_COMPAT_VERSION = "0.118-static"
DRAFTS_FILE = "promptfoo_drafts.json"

#: Import safety limits.
MAX_IMPORT_BYTES = ext.MAX_IMPORT_BYTES
MAX_LINES = 50_000
MAX_DEPTH = ext.MAX_JSON_DEPTH
MAX_STRING_LENGTH = 2_000
MAX_VARS = 100
MAX_TESTS = 1_000
MAX_PROVIDERS = 20

#: Keys whose values are executable or otherwise unsafe content. They are
#: never imported as-is; presence is recorded as a warning and the content is
#: dropped (or the assertion preserved as ``unmapped``).
FORBIDDEN_KEYS = (
    "command",
    "plugin",
    "plugins",
    "shell",
    "exec",
    "javascript",
    "python",
    "function",
    "transform",
    "webhook",
)

#: Assertion types that map to safe, static expectation kinds.
_SAFE_ASSERT_MAP = {
    "equals": "static_equals",
    "icontains": "static_icontains",
    "contains": "static_contains",
    "not-contains": "static_not_contains",
    "not-icontains": "static_not_icontains",
    "regex": "static_regex",
    "not-regex": "static_not_regex",
    "starts-with": "static_starts_with",
    "is-json": "static_is_json",
}


class PromptfooDependencyError(ValueError):
    """Raised when YAML input is provided but no safe YAML parser exists."""


# ---------------------------------------------------------------------------
# safe parsing
# ---------------------------------------------------------------------------


def _load_yaml_module():
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise PromptfooDependencyError(
            "YAML 解析需要安装 PyYAML（安全解析器 yaml.safe_load）；"
            "当前环境未安装，请改用 JSON 格式的 promptfoo 配置或安装 PyYAML"
        ) from exc
    return yaml


def parse_config_text(text: str, *, source_name: str = "<inline>") -> Dict[str, Any]:
    """Parse a promptfoo config (YAML via safe_load, or JSON). Raises
    ``ValueError``/``PromptfooDependencyError``; the message never embeds raw
    parser internals beyond the exception type."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("promptfoo config is empty")
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        raise ValueError("promptfoo config exceeds the %d line limit" % MAX_LINES)
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("promptfoo config is not valid JSON: line %d" % getattr(exc, "lineno", 0)) from exc
    else:
        yaml = _load_yaml_module()
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = "line %s" % getattr(mark, "line", "?") if mark is not None else "unknown position"
            raise ValueError("promptfoo config is not valid YAML (%s)" % where) from exc
    if not isinstance(payload, dict):
        raise ValueError("promptfoo config must be a mapping at the top level")
    if ext._json_depth(payload) > MAX_DEPTH + 4:
        raise ValueError("promptfoo config exceeds the maximum nesting depth")
    return payload


def read_config_file(path: Path) -> Dict[str, Any]:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("promptfoo config is not readable: %s" % type(exc).__name__) from exc
    if size > MAX_IMPORT_BYTES:
        raise ValueError("promptfoo config exceeds the %d byte limit" % MAX_IMPORT_BYTES)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("promptfoo config is not readable: %s" % type(exc).__name__) from exc
    return parse_config_text(text, source_name=path.name)


def _truncate(value: Any, *, limit: int = MAX_STRING_LENGTH) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_truncate(item, limit=limit) for item in value[:MAX_VARS]]
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_VARS]:
            cleaned[str(key)[:64]] = _truncate(item, limit=limit)
        return cleaned
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:limit]


# ---------------------------------------------------------------------------
# provider / assertion / test mapping
# ---------------------------------------------------------------------------


def map_provider(raw: Any, warnings: List[str]) -> Optional[Dict[str, Any]]:
    """Map one promptfoo provider entry to a *declaration only*. Executable
    provider configs (command/plugin/function) are rejected with a warning."""
    if isinstance(raw, str):
        return {"id": raw[: ext.MAX_FIELD_LENGTH], "executable": False}
    if not isinstance(raw, dict):
        warnings.append("provider entry is not a string or object and was skipped")
        return None
    lowered = {str(key).lower() for key in raw}
    forbidden = sorted(lowered & set(FORBIDDEN_KEYS))
    if forbidden:
        warnings.append(
            "provider declares executable content (%s); declaration skipped" % ", ".join(forbidden)
        )
        return None
    provider_id = raw.get("id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        warnings.append("provider object is missing a string id and was skipped")
        return None
    declaration: Dict[str, Any] = {"id": provider_id.strip()[: ext.MAX_FIELD_LENGTH], "executable": False}
    label = raw.get("label")
    if isinstance(label, str):
        declaration["label"] = label[: ext.MAX_FIELD_LENGTH]
    return declaration


def map_assertion(raw: Any, index: int, warnings: List[str]) -> Dict[str, Any]:
    """Map one promptfoo assertion. Unsafe or unknown assertion types are
    preserved as ``unmapped`` — never silently dropped."""
    if not isinstance(raw, dict):
        return {
            "kind": "unmapped",
            "original_type": type(raw).__name__,
            "reason": "assertion is not an object",
            "safe_to_execute": False,
        }
    assert_type = str(raw.get("type") or "").strip()
    if assert_type in _SAFE_ASSERT_MAP:
        return {
            "kind": _SAFE_ASSERT_MAP[assert_type],
            "original_type": assert_type,
            "value": _truncate(raw.get("value")),
            "safe_to_execute": True,
        }
    reason = "assertion type %r is not a safe static assertion" % (assert_type or "unknown")
    if assert_type in FORBIDDEN_KEYS or assert_type.startswith(("javascript", "python", "model-", "llm", "webhook")):
        reason = "assertion type %r requires execution or an external grader and was not imported" % assert_type
    return {
        "kind": "unmapped",
        "original_type": assert_type or "unknown",
        "reason": reason,
        "safe_to_execute": False,
    }


def map_test(raw: Any, index: int, warnings: List[str]) -> Dict[str, Any]:
    """Map one promptfoo test entry to a draft TestCase payload. Raises
    ``ValueError`` only when the entry is not an object at all."""
    if not isinstance(raw, dict):
        raise ValueError("test entry %d is not an object" % index)

    lowered = {str(key).lower() for key in raw}
    forbidden = sorted(lowered & set(FORBIDDEN_KEYS))
    for key in forbidden:
        warnings.append("test %d declares executable content (%s); content dropped" % (index, key))

    description = raw.get("description")
    vars_raw = raw.get("vars") if isinstance(raw.get("vars"), dict) else {}
    if len(vars_raw) > MAX_VARS:
        warnings.append("test %d has more than %d vars; extra vars dropped" % (index, MAX_VARS))
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

    assertions_raw = raw.get("assert") or raw.get("assertions") or []
    if not isinstance(assertions_raw, list):
        warnings.append("test %d assert block is not a list and was skipped" % index)
        assertions_raw = []
    assertions = [map_assertion(item, position, warnings) for position, item in enumerate(assertions_raw, start=1)]
    unmapped = sum(1 for item in assertions if item.get("kind") == "unmapped")

    title = str(description or "").strip()[: ext.MAX_FIELD_LENGTH]
    if not title:
        title = "promptfoo test %d" % index
    digest = hashlib.sha256(
        json.dumps(_truncate(raw), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    case_id = str(metadata.get("case_id") or metadata.get("id") or "").strip()[:128]
    if not case_id:
        case_id = "promptfoo-case-%s" % digest

    safe_to_execute = unmapped == 0 and not forbidden
    draft = {
        "case_id": case_id,
        "title": title,
        "draft": True,
        "safe_to_execute": safe_to_execute,
        "source_system": SOURCE_SYSTEM,
        "source_index": index,
        "vars": _truncate(vars_raw),
        "expectations": assertions,
        "provider_declaration": None,
        "metadata": redact_metadata(metadata),
        "unmapped_count": unmapped,
    }
    if not safe_to_execute:
        warnings.append(
            "test %d (%s) is a draft with %d unmapped assertion(s); it cannot enter a production SuiteJob"
            % (index, case_id, unmapped)
        )
    return draft


# ---------------------------------------------------------------------------
# import pipeline
# ---------------------------------------------------------------------------


def import_config(
    platform_dir: Path,
    config: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Import one parsed promptfoo config; returns the import report.

    Produces draft TestCase payloads only — nothing is executed, no provider
    is called and no real platform TestCase is created implicitly. A single
    broken test entry yields a per-item error and never aborts the import.
    """
    if not isinstance(config, dict):
        raise ValueError("promptfoo config must be an object")
    report = ext.new_import_report(SOURCE_SYSTEM, dry_run=dry_run)
    report["kind"] = "promptfoo_config"
    warnings: List[str] = []

    providers_raw = config.get("providers") or config.get("provider") or []
    if isinstance(providers_raw, (str, dict)):
        providers_raw = [providers_raw]
    if not isinstance(providers_raw, list):
        ext.record_import_warning(report, "providers block is not a list and was skipped")
        providers_raw = []
    providers: List[Dict[str, Any]] = []
    for item in providers_raw[:MAX_PROVIDERS]:
        declaration = map_provider(item, warnings)
        if declaration is not None:
            providers.append(declaration)
    for message in warnings[: ext.MAX_IMPORT_ERRORS]:
        ext.record_import_warning(report, message)

    tests_raw = config.get("tests") or []
    if isinstance(tests_raw, dict):
        tests_raw = [tests_raw]
    if not isinstance(tests_raw, list):
        raise ValueError("promptfoo config tests must be a list")
    if len(tests_raw) > MAX_TESTS:
        ext.record_import_warning(report, "config has more than %d tests; extra tests skipped" % MAX_TESTS)
        tests_raw = tests_raw[:MAX_TESTS]

    drafts: List[Dict[str, Any]] = []
    for index, raw in enumerate(tests_raw, start=1):
        report["total_records"] += 1
        item_warnings: List[str] = []
        try:
            draft = map_test(raw, index, item_warnings)
        except ValueError as exc:
            ext.record_import_error(report, index, str(exc))
            continue
        for message in item_warnings:
            ext.record_import_warning(report, message)
        report["imported_records"] += 1
        if not draft.get("safe_to_execute"):
            report["skipped_records"] += 1
        drafts.append(draft)

    default_provider = providers[0] if providers else None
    for draft in drafts:
        if draft.get("provider_declaration") is None:
            draft["provider_declaration"] = default_provider

    if dry_run:
        report["status"] = "dry_run"
        report["draft_cases"] = drafts[:50]
        return report

    existing = _load_drafts(platform_dir)
    merged, duplicates, merge_warnings = ext.merge_external_records(
        [
            {**draft, "source_id": draft.get("case_id"), "source_system": SOURCE_SYSTEM}
            for draft in existing
        ],
        [
            {**draft, "source_id": draft.get("case_id"), "source_system": SOURCE_SYSTEM}
            for draft in drafts
        ],
        id_field="promptfoo draft",
    )
    report["duplicate_records"] = duplicates
    report["imported_records"] -= duplicates
    for message in merge_warnings:
        ext.record_import_warning(report, message)
    _save_drafts(platform_dir, merged)
    report["draft_count"] = len(drafts)
    ext.finalize_import_report(report)
    ext.save_external_import_report(platform_dir, report)
    return report


def import_file(
    platform_dir: Path,
    source_path: Path,
    *,
    allowed_roots: List[Path],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Resolve, parse and import one promptfoo config file."""
    path = resolve_import_path(source_path, allowed_roots)
    config = read_config_file(path)
    return import_config(platform_dir, config, dry_run=dry_run)


def _drafts_path(platform_dir: Path) -> Path:
    return Path(platform_dir) / ext.EXTERNAL_DIR / DRAFTS_FILE


def _load_drafts(platform_dir: Path) -> List[Dict[str, Any]]:
    path = _drafts_path(platform_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _save_drafts(platform_dir: Path, drafts: List[Dict[str, Any]]) -> None:
    from ..trajectory import write_json

    path = _drafts_path(platform_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"schema_version": ext.SCHEMA_VERSION, "items": drafts, "updated_at": utc_now()})


def load_drafts(platform_dir: Path) -> List[Dict[str, Any]]:
    return _load_drafts(platform_dir)


# ---------------------------------------------------------------------------
# safe export
# ---------------------------------------------------------------------------


def export_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """Render one platform TestCase as a safe static promptfoo config (JSON).

    The output contains only static, non-sensitive fields: a provider
    *declaration* (never endpoint secrets), truncated vars/inputs and static
    assertions. It never includes tokens, cookies, Authorization headers or
    full private prompts, and it never modifies the input case.
    """
    if not isinstance(case, dict):
        raise ValueError("test case must be an object")
    case_id = str(case.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("test case is missing case_id")

    input_spec = case.get("input") if isinstance(case.get("input"), dict) else {}
    vars_out: Dict[str, Any] = {}
    for key in ("user_id", "session_id"):
        value = input_spec.get(key)
        if isinstance(value, str) and value.strip():
            vars_out[key] = value.strip()[: ext.MAX_FIELD_LENGTH]
    message = input_spec.get("message")
    if isinstance(message, str) and message.strip():
        # Never export a full private prompt: bound the excerpt.
        vars_out["message_excerpt"] = message.strip()[: ext.MAX_COMMENT_LENGTH]

    expectations = case.get("expectations") if isinstance(case.get("expectations"), list) else []
    assertions: List[Dict[str, Any]] = []
    for item in expectations[:MAX_VARS]:
        if not isinstance(item, dict):
            continue
        assertions.append(
            {
                "type": "auditrail:%s" % str(item.get("type") or "unknown")[:64],
                "id": str(item.get("id") or "")[:128],
                "required": bool(item.get("required", True)),
            }
        )

    adapter_config = case.get("adapter_config") if isinstance(case.get("adapter_config"), dict) else {}
    provider_id = str(adapter_config.get("provider_id") or case.get("adapter") or "unknown")[: ext.MAX_FIELD_LENGTH]

    return {
        "description": str(case.get("title") or case_id)[: ext.MAX_FIELD_LENGTH],
        "promptfoo_compatibility": PROMPTFOO_COMPAT_VERSION,
        "metadata": {
            "platform": "auditrail",
            "platform_schema_version": 1,
            "case_id": case_id,
            "case_version": case.get("version"),
            "exported_at": utc_now(),
            "note": "静态导出：不包含密钥、token、Authorization header 或完整私密 prompt",
        },
        "providers": [{"id": provider_id, "label": "declaration-only"}],
        "tests": [
            {
                "description": str(case.get("title") or case_id)[: ext.MAX_FIELD_LENGTH],
                "vars": vars_out,
                "assert": assertions,
                "metadata": {"case_id": case_id, "draft": False},
            }
        ],
    }
