"""Selectable judge engines for Auditrail runs.

Engines produce an untrusted report proposal. Normalization, evidence
validation, deterministic caps, quality assessment, and report writing stay in
``auditrail.agents.tools.write_judge_report``; no engine may write final report
files directly.

``reports`` stores one normalized report per engine under ``judges/<engine>/``
with a ``judges/manifest.json`` index, so a run can keep the DeepAgent and DSH
reports side by side while the root ``agent_judge.json`` stays the active,
backward-compatible report.
"""

from .base import (
    DEFAULT_JUDGE_ENGINE,
    DEEPAGENT_ENGINE,
    DSH_ENGINE,
    JudgeEngine,
    JudgeEngineError,
    JudgeExecutionError,
    JudgeRequest,
    JudgeResult,
    JudgeUnavailableError,
    default_judge_engine_id,
    describe_engines,
    judge_engine_ids,
    list_judge_engines,
    resolve_judge_engine,
)
from .reports import (
    JudgeReportLockTimeout,
    JudgeReportSnapshotUnstable,
    available_judge_engines,
    compare_judge_reports,
    list_judge_reports,
    normalize_engine_id,
    publish_judge_report,
    read_judge_manifest,
    read_judge_report,
    set_active_judge_report,
)

__all__ = [
    "DEFAULT_JUDGE_ENGINE",
    "DEEPAGENT_ENGINE",
    "DSH_ENGINE",
    "JudgeEngine",
    "JudgeEngineError",
    "JudgeExecutionError",
    "JudgeReportLockTimeout",
    "JudgeReportSnapshotUnstable",
    "JudgeRequest",
    "JudgeResult",
    "JudgeUnavailableError",
    "available_judge_engines",
    "compare_judge_reports",
    "default_judge_engine_id",
    "describe_engines",
    "judge_engine_ids",
    "list_judge_engines",
    "list_judge_reports",
    "normalize_engine_id",
    "publish_judge_report",
    "read_judge_manifest",
    "read_judge_report",
    "resolve_judge_engine",
    "set_active_judge_report",
]
