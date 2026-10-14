"""Multi-judge report storage for Auditrail runs.

One run can hold a report per judge engine. The layout is:

```text
runs/<run_id>/
  agent_judge.json|md      # active, backward-compatible report (newest publish)
  chart_data.json
  judges/
    manifest.json
    deepagent/agent_judge.json|md, chart_data.json
    dsh/agent_judge.json|md, chart_data.json
```

This module is deterministic and credential-free: it stores already-normalized
reports, it never normalizes or renders one. ``write_judge_report`` stays the
only path that turns a raw proposal into report files; it archives its output
here.

Atomic publishing: every file is staged in the target directory and moved into
place with ``os.link``, which either creates a complete file or fails, so a
reader never observes a truncated report. The manifest is written after the
report files and is only accepted as active while it is at least as new as
every file it points at; a reader that observes a newer file treats the
snapshot as in-flight and re-reads instead of mixing engines.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

_ENGINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENGINE_ALIASES = {"deepagent": "deepagent", "dsh": "dsh"}
_ACTIVE_ENGINE_FALLBACK = "deepagent"
_JUDGES_DIR = "judges"
_MANIFEST_NAME = "manifest.json"

REPORT_FILENAMES = {
    "json_path": "agent_judge.json",
    "markdown_path": "agent_judge.md",
    "chart_path": "chart_data.json",
}

_LOCK_NAME = ".judges.lock"
_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_STALE_SECONDS = 300.0
_LOCK_POLL_SECONDS = 0.02

READ_ATTEMPTS = 5
READ_BACKOFF_SECONDS = 0.02

_MANIFEST_VERSION = 1
_ALLOWED_SEVERITY = ("critical", "high", "medium", "low")

# Comparison status ranking used to validate dimension statuses. ``unknown``
# ranks lowest on purpose: it never counts as a disagreement with a real
# judgment and never reads as a failure.
_STATUS_RANK = {"unknown": 0, "pass": 1, "partial": 2, "fail": 3}


class JudgeReportLockTimeout(RuntimeError):
    """The run's judge-report lock stayed busy for longer than the timeout."""


class JudgeReportSnapshotUnstable(RuntimeError):
    """A report snapshot stayed mid-publish; callers surface it instead of mixing."""


# ---------------------------------------------------------------- engine ids


def normalize_engine_id(value: object) -> str:
    """Normalize and validate one judge engine id.

    Engine ids become directory names, so anything that is not a short
    alphanumeric identifier (``..``, slashes, empty) is rejected here.
    """
    text = str(value or "").strip().lower()
    text = _ENGINE_ALIASES.get(text, text)
    if not _ENGINE_ID_RE.fullmatch(text):
        raise ValueError("invalid judge engine id")
    return text


def report_relative_dir(engine_id: object) -> str:
    """Return the run-relative archive directory for one engine."""
    return "%s/%s" % (_JUDGES_DIR, normalize_engine_id(engine_id))


# ------------------------------------------------------------------- paths


def judge_root(run_path: str | Path) -> Path:
    return Path(run_path) / _JUDGES_DIR


def manifest_path(run_path: str | Path) -> Path:
    return judge_root(run_path) / _MANIFEST_NAME


def report_relative_paths(engine_id: object) -> dict[str, str]:
    """Return the run-relative report file paths for one engine."""
    base = report_relative_dir(engine_id)
    return {key: "%s/%s" % (base, name) for key, name in REPORT_FILENAMES.items()}


def resolve_report_path(run_path: str | Path, relative: object) -> Path:
    """Resolve one stored report path, rejecting engine ids that escape the run.

    Bare root filenames (the legacy single-report layout) are accepted; engine
    archives must be exactly ``judges/<engine>/<known report file>``.
    """
    text = str(relative or "").strip()
    if not text or text.startswith("/") or "\\" in text:
        raise ValueError("invalid judge report path")
    parts = text.split("/")
    if len(parts) == 1:
        if parts[0] not in REPORT_FILENAMES.values():
            raise ValueError("invalid judge report path")
        return Path(run_path) / parts[0]
    if parts[0] != _JUDGES_DIR or len(parts) != 3 or parts[2] not in REPORT_FILENAMES.values():
        raise ValueError("invalid judge report path")
    concrete = (Path(run_path) / Path(*parts)).resolve(strict=False)
    # Revalidate every path component after resolution so symlinks cannot escape.
    root = judge_root(run_path).resolve(strict=False)
    if root != concrete.parent.parent:
        raise ValueError("invalid judge report path")
    normalize_engine_id(parts[1])
    return concrete


# ------------------------------------------------------------------ atomic IO


@contextmanager
def judge_report_lock(run_path: str | Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialize judge-report publishing for one run directory.

    The lock is a file created with ``O_EXCL``. A holder that died is detected
    by mtime age so a crashed writer cannot block judging forever.
    """
    run_dir = Path(run_path)
    root = judge_root(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock_file = root / _LOCK_NAME
    deadline = time.monotonic() + max(0.0, float(timeout))
    handle: Optional[int] = None
    while handle is None:
        try:
            handle = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(lock_file):
                _clear_lock(lock_file)
                continue
            if time.monotonic() >= deadline:
                raise JudgeReportLockTimeout("timed out waiting for the judge report lock")
            time.sleep(min(_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    try:
        yield
    finally:
        os.close(handle)
        _clear_lock(lock_file)


def _lock_is_stale(lock_file: Path) -> bool:
    try:
        age = time.time() - lock_file.stat().st_mtime
    except OSError:
        return True
    return age > _LOCK_STALE_SECONDS


def _clear_lock(lock_file: Path) -> None:
    try:
        lock_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _write_atomic(path: Path, data: str) -> None:
    """Create ``path`` completely or not at all, and never overwrite a peer's write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / (".%s.%d.%d.tmp" % (path.name, os.getpid(), int(time.time() * 1e6)))
    try:
        with open(staging, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(str(staging), str(path))
        except FileExistsError:
            return
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.EACCES, errno.EMLINK):
                raise
            if not path.exists():
                shutil.copyfile(str(staging), str(path))
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def replace_report_file(path: Path, data: str) -> None:
    """Replace ``path`` with complete content; readers only ever see one or the other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / (".%s.%d.%d.tmp" % (path.name, os.getpid(), int(time.time() * 1e6)))
    try:
        with open(staging, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(staging, path)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def _replace_with_retry(source: Path, target: Path, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            os.replace(str(source), str(target))
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(_LOCK_POLL_SECONDS)


# ----------------------------------------------------------------- publish


def publish_judge_report(
    run_path: str | Path,
    report: Mapping[str, Any],
    *,
    engine: object,
    markdown: str,
    charts: Mapping[str, Any],
    published_at: Optional[str] = None,
) -> dict[str, str]:
    """Archive one already-normalized report for an engine and activate it.

    ``report`` must be the normalized document produced by
    ``auditrail.agents.tools.write_judge_report``; this function only stores it.
    """
    run_dir = Path(run_path)
    engine_id = normalize_engine_id(engine)
    files = report_relative_paths(engine_id)
    payloads = {
        "json_path": json.dumps(report, ensure_ascii=False, indent=2),
        "markdown_path": str(markdown),
        "chart_path": json.dumps(charts, ensure_ascii=False, indent=2),
    }
    with judge_report_lock(run_dir):
        active_at = _monotonic_timestamp(run_dir, published_at)
        for relative, payload in (
            (files["json_path"], payloads["json_path"]),
            (files["markdown_path"], payloads["markdown_path"]),
            (files["chart_path"], payloads["chart_path"]),
        ):
            _write_atomic(run_dir / relative, payload)
        for key in REPORT_FILENAMES:
            replace_report_file(run_dir / REPORT_FILENAMES[key], payloads[key])
        manifest = _manifest_after_publish(run_dir, engine_id, files, active_at)
        replace_report_file(manifest_path(run_dir), json.dumps(manifest, ensure_ascii=False, indent=2))
    return {
        "json_path": str(run_dir / REPORT_FILENAMES["json_path"]),
        "markdown_path": str(run_dir / REPORT_FILENAMES["markdown_path"]),
        "chart_path": str(run_dir / REPORT_FILENAMES["chart_path"]),
        "archive_json_path": str(run_dir / files["json_path"]),
        "archive_markdown_path": str(run_dir / files["markdown_path"]),
        "archive_chart_path": str(run_dir / files["chart_path"]),
        "manifest_path": str(manifest_path(run_dir)),
    }


def _monotonic_timestamp(run_dir: Path, published_at: Optional[str]) -> str:
    """Keep manifest timestamps strictly increasing so archive never looks newer."""
    requested = _normalize_timestamp(published_at)
    current = _read_manifest(run_dir)
    latest = requested
    for report in current.get("reports") or []:
        candidate = str(report.get("published_at") or "")
        if candidate > latest:
            latest = candidate
    return latest


def _normalize_timestamp(value: Optional[str]) -> str:
    text = str(value or "").strip()
    if text:
        return text
    return datetime.now(timezone.utc).isoformat()


def _manifest_after_publish(
    run_dir: Path,
    engine_id: str,
    files: Mapping[str, str],
    published_at: str,
) -> dict[str, Any]:
    manifest = _read_manifest(run_dir)
    entry = {
        "engine": engine_id,
        "available": True,
        "published_at": published_at,
        **files,
    }
    reports = [
        report
        for report in manifest.get("reports") or []
        if isinstance(report, dict) and report.get("engine") != engine_id
    ]
    reports.insert(0, entry)
    reports.sort(key=lambda item: (str(item.get("engine") or ""),))
    return {
        "version": _MANIFEST_VERSION,
        "active_engine": engine_id,
        "reports": reports,
    }


# -------------------------------------------------------------------- read


def read_judge_manifest(run_path: str | Path) -> dict[str, Any]:
    """Return the canonical manifest, including a legacy single-report view."""
    run_dir = Path(run_path)
    last_error: Optional[Exception] = None
    for attempt in range(READ_ATTEMPTS):
        try:
            return _read_manifest_snapshot(run_dir)
        except JudgeReportSnapshotUnstable as exc:  # a peer is mid-publish
            last_error = exc
            time.sleep(READ_BACKOFF_SECONDS * (attempt + 1))
    raise last_error or JudgeReportSnapshotUnstable("judge report snapshot is unstable")


def list_judge_reports(run_path: str | Path) -> dict[str, Any]:
    """Describe every stored judge report for the run, active one included."""
    return read_judge_manifest(run_path)


def available_judge_engines(run_path: str | Path) -> list[str]:
    manifest = read_judge_manifest(run_path)
    engines = []
    for report in manifest.get("reports") or []:
        if not isinstance(report, dict) or not report.get("available"):
            continue
        engine = str(report.get("engine") or "").strip()
        if engine and engine not in engines:
            engines.append(engine)
    return engines


def read_judge_report(run_path: str | Path, engine: object) -> dict[str, Any]:
    """Load one stored report, falling back to the legacy root report."""
    run_dir = Path(run_path)
    engine_id = normalize_engine_id(engine)
    last_error: Optional[Exception] = None
    for attempt in range(READ_ATTEMPTS):
        try:
            return _read_report_snapshot(run_dir, engine_id)
        except JudgeReportSnapshotUnstable as exc:
            last_error = exc
            time.sleep(READ_BACKOFF_SECONDS * (attempt + 1))
    raise last_error or JudgeReportSnapshotUnstable("judge report snapshot is unstable")


def set_active_judge_report(
    run_path: str | Path,
    engine: object,
    *,
    published_at: Optional[str] = None,
) -> dict[str, Any]:
    """Make one stored engine report the active root report again."""
    run_dir = Path(run_path)
    engine_id = normalize_engine_id(engine)
    with judge_report_lock(run_dir):
        manifest = _read_manifest_snapshot(run_dir)
        report_files = _manifest_entry_files(manifest, engine_id)
        if report_files is None:
            raise FileNotFoundError("run has no stored judge report for engine %s" % engine_id)
        json_path = _required_report_file(run_dir, report_files.get("json_path"))
        markdown_path = _required_report_file(run_dir, report_files.get("markdown_path"))
        chart_path = _required_report_file(run_dir, report_files.get("chart_path"))
        replace_report_file(run_dir / REPORT_FILENAMES["json_path"], json_path.read_text(encoding="utf-8"))
        replace_report_file(run_dir / REPORT_FILENAMES["markdown_path"], markdown_path.read_text(encoding="utf-8"))
        replace_report_file(run_dir / REPORT_FILENAMES["chart_path"], chart_path.read_text(encoding="utf-8"))
        active_at = _monotonic_timestamp(run_dir, published_at)
        entry = _manifest_entry(manifest, engine_id) or {}
        entry = {**entry, "engine": engine_id, "published_at": active_at}
        reports = [
            report
            for report in manifest.get("reports") or []
            if isinstance(report, dict) and report.get("engine") != engine_id
        ]
        reports.insert(0, entry)
        reports.sort(key=lambda item: (str(item.get("engine") or ""),))
        updated = {"version": _MANIFEST_VERSION, "active_engine": engine_id, "reports": reports}
        replace_report_file(manifest_path(run_dir), json.dumps(updated, ensure_ascii=False, indent=2))
        return _canonical_manifest(updated)


def _required_report_file(run_dir: Path, relative: object) -> Path:
    path = resolve_report_path(run_dir, relative)
    if not path.is_file():
        raise FileNotFoundError("missing judge report file %s" % relative)
    return path


# ------------------------------------------------------------------ compare


def compare_judge_reports(run_path: str | Path) -> dict[str, Any]:
    """Compare every stored judge report without choosing a winner.

    Unknown statuses rank below real judgments, so a dimension one engine never
    assessed is reported as an open question instead of a disagreement or a
    failure. Deterministic signals (evidence validity, quality caps, facts
    downgraded by evidence validation) are reported per engine so a semantic
    pass can never hide a deterministic failure.
    """
    run_dir = Path(run_path)
    last_error: Optional[Exception] = None
    for attempt in range(READ_ATTEMPTS):
        try:
            return _compare_snapshot(run_dir)
        except JudgeReportSnapshotUnstable as exc:
            last_error = exc
            time.sleep(READ_BACKOFF_SECONDS * (attempt + 1))
    raise last_error or JudgeReportSnapshotUnstable("judge report snapshot is unstable")


def _compare_snapshot(run_dir: Path) -> dict[str, Any]:
    manifest = _read_manifest_snapshot(run_dir)
    engines: list[str] = []
    reports: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("reports") or []:
        if not isinstance(entry, dict) or not entry.get("available"):
            continue
        engine = normalize_engine_id(entry.get("engine"))
        if engine in engines:
            continue
        report = _read_report_for_entry(run_dir, entry)
        if report is None:  # a listed archive that is gone is simply not comparable
            continue
        reports[engine] = report
        engines.append(engine)
    engines.sort()
    rows = [_engine_row(engine, reports[engine]) for engine in engines]
    return {
        "active_engine": str(manifest.get("active_engine") or ""),
        "engines": engines,
        "overall_status": _overall_rows(engines, reports),
        "completion_score": _completion_rows(engines, reports),
        "reliability": _reliability_rows(engines, reports),
        "dimensions": _dimension_rows(engines, reports),
        "disagreements": _disagreement_rows(rows),
        "deterministic_findings": _deterministic_finding_rows(reports),
        "judge_finding_counts": _judge_finding_rows(engines, reports),
        "fact_inference_mismatch": _mismatch_rows(engines, reports),
    }


def _read_report_for_entry(run_dir: Path, entry: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    try:
        json_path = _required_report_file(run_dir, entry.get("json_path"))
    except (ValueError, FileNotFoundError):
        return None
    return _load_report(json_path)


def _engine_row(engine: str, report: Mapping[str, Any]) -> dict[str, Any]:
    reliability = report.get("judge_reliability") or {}
    if not isinstance(reliability, dict):
        reliability = {}
    return {
        "engine": engine,
        "overall_status": str(report.get("overall_status") or report.get("status") or "unknown"),
        "completion_score": _int_or(report.get("completion_score"), 0),
        "confidence": str(report.get("confidence") or "low"),
        "evidence_coverage": _float_or(reliability.get("evidence_coverage"), 0.0),
        "invalid_evidence_count": _int_or(reliability.get("invalid_evidence_count"), 0),
        "fact_inference_mismatch_count": _int_or(reliability.get("fact_inference_mismatch_count"), 0),
        "finding_count": len([item for item in report.get("findings") or [] if isinstance(item, dict)]),
    }


def _overall_rows(engines: list[str], reports: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_engine = {engine: str(reports[engine].get("overall_status") or "unknown") for engine in engines}
    distinct = sorted({value for value in by_engine.values()})
    return {"by_engine": by_engine, "disagreement": len(distinct) > 1}


def _completion_rows(engines: list[str], reports: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_engine = {engine: _int_or(reports[engine].get("completion_score"), 0) for engine in engines}
    scores = sorted(by_engine.values())
    return {
        "by_engine": by_engine,
        "spread": (scores[-1] - scores[0]) if scores else 0,
        "disagreement": len(set(scores)) > 1,
    }


def _reliability_rows(engines: list[str], reports: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_engine: dict[str, dict[str, Any]] = {}
    for engine in engines:
        reliability = reports[engine].get("judge_reliability") or {}
        if not isinstance(reliability, dict):
            reliability = {}
        by_engine[engine] = {
            "evidence_coverage": _float_or(reliability.get("evidence_coverage"), 0.0),
            "invalid_evidence_count": _int_or(reliability.get("invalid_evidence_count"), 0),
            "fact_inference_mismatch_count": _int_or(reliability.get("fact_inference_mismatch_count"), 0),
            "valid_evidence_count": _int_or(reliability.get("valid_evidence_count"), 0),
            "total_evidence_count": _int_or(reliability.get("total_evidence_count"), 0),
        }
    return by_engine


def _dimension_rows(engines: list[str], reports: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    keys: list[str] = []
    for engine in engines:
        for key in (reports[engine].get("dimensions") or {}):
            if key not in keys:
                keys.append(key)
    keys.sort()
    rows: dict[str, Any] = {}
    for key in keys:
        status_by_engine: dict[str, str] = {}
        score_by_engine: dict[str, Any] = {}
        for engine in engines:
            dimension = (reports[engine].get("dimensions") or {}).get(key)
            if not isinstance(dimension, dict):
                status_by_engine[engine] = "unknown"
                score_by_engine[engine] = None
                continue
            status_by_engine[engine] = _dimension_status(dimension.get("status"))
            score_by_engine[engine] = _int_or(dimension.get("score"), 0)
        known = {status for status in status_by_engine.values() if status != "unknown"}
        unknown_engines = sorted(engine for engine, status in status_by_engine.items() if status == "unknown")
        rows[key] = {
            "status": status_by_engine,
            "score": score_by_engine,
            "disagreement": len(known) > 1,
            "unknown_engines": unknown_engines,
        }
    return rows


def _disagreement_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cross-engine verdict differences; never collapses to one winner."""
    if len(rows) < 2:
        return []
    items: list[dict[str, Any]] = []
    statuses = {row["engine"]: row["overall_status"] for row in rows}
    if len(set(statuses.values())) > 1:
        items.append({"kind": "overall_status", "by_engine": statuses})
    scores = {row["engine"]: row["completion_score"] for row in rows}
    if len(set(scores.values())) > 1:
        items.append({"kind": "completion_score", "by_engine": scores})
    return items


def _deterministic_finding_rows(reports: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Facts that survived evidence validation, per engine.

    These are the claims a semantic judge made; they stay visible next to the
    deterministic quality findings so a passing judge cannot bury them.
    """
    by_engine: dict[str, Any] = {}
    for engine in sorted(reports):
        facts: list[dict[str, Any]] = []
        for finding in reports[engine].get("findings") or []:
            if not isinstance(finding, dict) or finding.get("type") != "fact":
                continue
            facts.append(
                {
                    "severity": _severity(finding.get("severity")),
                    "category": str(finding.get("category") or ""),
                    "title": str(finding.get("title") or ""),
                }
            )
        facts.sort(key=lambda item: (_severity_rank(item["severity"]), item["category"], item["title"]))
        counts = Counter(item["severity"] for item in facts)
        by_engine[engine] = {
            "count": len(facts),
            "severity_counts": [{"severity": name, "count": counts[name]} for name in _ALLOWED_SEVERITY if counts[name]],
            "findings": facts,
        }
    return by_engine


def _judge_finding_rows(engines: list[str], reports: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_engine: dict[str, Any] = {}
    for engine in engines:
        findings = [item for item in reports[engine].get("findings") or [] if isinstance(item, dict)]
        counts = Counter(_severity(item.get("severity")) for item in findings)
        by_engine[engine] = {
            "total": len(findings),
            "by_severity": {name: counts[name] for name in _ALLOWED_SEVERITY if counts[name]},
        }
    return by_engine


def _mismatch_rows(engines: list[str], reports: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_engine: dict[str, Any] = {}
    for engine in engines:
        reliability = reports[engine].get("judge_reliability") or {}
        if not isinstance(reliability, dict):
            reliability = {}
        notes = reliability.get("notes") or []
        by_engine[engine] = {
            "count": _int_or(reliability.get("fact_inference_mismatch_count"), 0),
            "notes": [str(note) for note in notes if isinstance(note, (str, int, float))],
        }
    return by_engine


def _severity(value: object) -> str:
    text = str(value or "low").strip().lower()
    return text if text in _ALLOWED_SEVERITY else "low"


def _severity_rank(severity: str) -> int:
    try:
        return _ALLOWED_SEVERITY.index(severity)
    except ValueError:
        return len(_ALLOWED_SEVERITY)


def _dimension_status(value: object) -> str:
    text = str(value or "unknown").strip().lower()
    return text if text in _STATUS_RANK else "unknown"


# --------------------------------------------------------- manifest snapshots


def _read_manifest(run_dir: Path) -> dict[str, Any]:
    """Read the manifest leniently for internal use (empty on absence/corruption)."""
    path = manifest_path(run_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_manifest_snapshot(run_dir: Path) -> dict[str, Any]:
    data = _read_manifest(run_dir)
    if not data or not data.get("reports"):
        return _legacy_manifest(run_dir)
    manifest = _canonical_manifest(data)
    if not _manifest_is_settled(run_dir, manifest):
        raise JudgeReportSnapshotUnstable("judge manifest is newer than its report files")
    return manifest


def _read_report_snapshot(run_dir: Path, engine_id: str) -> dict[str, Any]:
    manifest = _read_manifest_snapshot(run_dir)
    entry = _manifest_entry(manifest, engine_id)
    if entry is None or not entry.get("available"):
        return {"status": "missing", "engine": engine_id}
    try:
        path = _required_report_file(run_dir, entry.get("json_path"))
        report = _load_report(path)
    except (ValueError, FileNotFoundError):
        return {"status": "missing", "engine": engine_id}
    return {**report, "judge_engine": engine_id}


def _legacy_manifest(run_dir: Path) -> dict[str, Any]:
    """Old runs only have root report files; expose them as one active report."""
    if not (run_dir / REPORT_FILENAMES["json_path"]).is_file():
        return {"version": _MANIFEST_VERSION, "active_engine": "", "reports": []}
    entry = {
        "engine": _ACTIVE_ENGINE_FALLBACK,
        "available": True,
        "published_at": "",
        "json_path": REPORT_FILENAMES["json_path"],
        "markdown_path": REPORT_FILENAMES["markdown_path"],
        "chart_path": REPORT_FILENAMES["chart_path"],
    }
    return {"version": _MANIFEST_VERSION, "active_engine": _ACTIVE_ENGINE_FALLBACK, "reports": [entry]}


def _canonical_manifest(data: Mapping[str, Any]) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for entry in data.get("reports") or []:
        if not isinstance(entry, dict):
            continue
        reports.append(dict(entry))
    reports.sort(key=lambda item: (str(item.get("engine") or ""),))
    active = str(data.get("active_engine") or "").strip()
    if not active or not any(str(entry.get("engine")) == active for entry in reports):
        active = str(reports[0].get("engine") or "") if reports else ""
    return {"version": _MANIFEST_VERSION, "active_engine": active, "reports": reports}


def _manifest_entry(manifest: Mapping[str, Any], engine_id: str) -> Optional[dict[str, Any]]:
    for entry in manifest.get("reports") or []:
        if isinstance(entry, dict) and str(entry.get("engine") or "") == engine_id:
            return entry
    return None


def _manifest_entry_files(manifest: Mapping[str, Any], engine_id: str) -> Optional[dict[str, Any]]:
    entry = _manifest_entry(manifest, engine_id)
    if entry is None:
        return None
    return {key: entry.get(key) for key in REPORT_FILENAMES}


def _manifest_is_settled(run_dir: Path, manifest: Mapping[str, Any]) -> bool:
    """True while no report file is newer than the manifest that points at it."""
    try:
        manifest_mtime = manifest_path(run_dir).stat().st_mtime
    except OSError:
        return False
    for entry in manifest.get("reports") or []:
        if not isinstance(entry, dict):
            continue
        for key in REPORT_FILENAMES:
            try:
                path = resolve_report_path(run_dir, entry.get(key))
            except ValueError:
                return False
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime > manifest_mtime:
                    return False
            except OSError:
                return False
    return True


def _load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("%s must contain a JSON object" % path.name)
    return data


def _int_or(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float_or(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
