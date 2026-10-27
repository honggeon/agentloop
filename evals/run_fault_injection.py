"""Fault-injection runner: measure the scoring pipeline's recall on known faults.

Usage:

    python3 evals/run_fault_injection.py --dir evals/fault-injection [--json]

Discovers ``fi-*.json`` samples (format contract v1, see
``evals/fault-injection/README.md``). Only ``reviewed`` samples are checked;
non-reviewed samples are listed separately, and samples whose run directory or
judge report is missing count as ``skipped`` without aborting the run.

Each fault declares at least one assertion:

- ``expect_status_at_most`` (pass > partial > fail): the judged dimension
  status must not exceed this band. ``unknown``/missing statuses carry no
  positive verdict and satisfy any band.
- ``expect_completion_score_at_most``: the report's ``completion_score`` must
  not exceed this ceiling; a null score is ``unverifiable`` (neither caught
  nor missed).

A fault is ``caught`` when every evaluated assertion holds, ``missed`` when
any assertion is violated, and ``unverifiable`` when no assertion could be
evaluated. Output is the recall ``caught / total`` plus a per-fault list of
what slipped through. This tool is read-only and deterministic: it consumes
existing ``agent_judge.json`` reports, no network, no model credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: pass > partial > fail；unknown/缺失不计入正向结论，任何档位上限都满足。
_STATUS_RANK = {"fail": 0, "unknown": 0, "partial": 1, "pass": 2}
_ALLOWED_EXPECT_STATUS = {"pass", "partial", "fail"}


def _load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolve_run_dir(run_dir: str) -> Path:
    """Resolve ``run_ref.run_dir`` (usually ``runs/<id>``, repo-relative)."""
    path = Path(run_dir).expanduser()
    if path.is_absolute() or path.exists():
        return path
    return Path(__file__).resolve().parent.parent / path


def evaluate_fault(fault: dict, report: dict) -> dict:
    """Evaluate one fault assertion set against a judge report.

    Returns ``{"outcome": caught|missed|unverifiable, "actual_status": ...,
    "actual_completion_score": ...}``.
    """
    expect_status = fault.get("expect_status_at_most")
    expect_score = fault.get("expect_completion_score_at_most")
    if expect_status is None and expect_score is None:
        raise ValueError("fault %r declares no assertion" % fault.get("id"))
    if expect_status is not None and expect_status not in _ALLOWED_EXPECT_STATUS:
        raise ValueError("fault %r has invalid expect_status_at_most %r" % (fault.get("id"), expect_status))

    dimension_key = str(fault.get("expect_dimension") or "")
    dimension = (report.get("dimensions") or {}).get(dimension_key)
    actual_status = str(dimension.get("status") or "unknown") if isinstance(dimension, dict) else "unknown"
    actual_score = report.get("completion_score")
    if actual_score is not None and not isinstance(actual_score, (int, float)):
        actual_score = None

    evaluated = []
    violated = []
    if expect_status is not None:
        rank = _STATUS_RANK.get(actual_status, 0)
        evaluated.append(True)
        if rank > _STATUS_RANK[expect_status]:
            violated.append(True)
    if expect_score is not None:
        if actual_score is None:
            pass  # unverifiable: no numeric score to compare
        else:
            evaluated.append(True)
            if float(actual_score) > float(expect_score):
                violated.append(True)

    if violated:
        outcome = "missed"
    elif evaluated:
        outcome = "caught"
    else:
        outcome = "unverifiable"
    return {
        "outcome": outcome,
        "actual_status": actual_status,
        "actual_completion_score": actual_score,
    }


def run_fault_injection(fi_dir: str | Path) -> dict:
    """Check every reviewed ``fi-*.json`` sample against its run's judge report."""
    fi_dir = Path(fi_dir)
    totals = {"samples": 0, "reviewed": 0, "skipped": 0, "faults": 0, "caught": 0, "missed": 0, "unverifiable": 0}
    non_reviewed = []
    skipped = []
    missed_faults = []
    unverifiable_faults = []

    for path in sorted(fi_dir.glob("fi-*.json")):
        totals["samples"] += 1
        try:
            sample = _load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            totals["skipped"] += 1
            skipped.append({"sample_id": path.stem, "reason": "unreadable sample: %s" % exc})
            continue
        sample_id = sample.get("sample_id") or path.stem
        if sample.get("labeling", {}).get("review_status") != "reviewed":
            non_reviewed.append(sample_id)
            continue
        totals["reviewed"] += 1
        run_dir = _resolve_run_dir(str((sample.get("run_ref") or {}).get("run_dir") or ""))
        report_path = run_dir / "agent_judge.json"
        if not run_dir.is_dir():
            totals["skipped"] += 1
            skipped.append({"sample_id": sample_id, "reason": "run directory not found: %s" % run_dir})
            continue
        if not report_path.exists():
            totals["skipped"] += 1
            skipped.append({"sample_id": sample_id, "reason": "missing %s" % report_path})
            continue
        try:
            report = _load_json(report_path)
            faults = list((sample.get("injection") or {}).get("faults") or [])
            results = [(fault, evaluate_fault(fault, report)) for fault in faults]
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            totals["skipped"] += 1
            skipped.append({"sample_id": sample_id, "reason": "unusable sample: %s" % exc})
            continue
        for fault, evaluation in results:
            totals["faults"] += 1
            totals[evaluation["outcome"]] += 1
            if evaluation["outcome"] == "caught":
                continue
            row = {
                "sample_id": sample_id,
                "fault_id": fault.get("id"),
                "dimension": fault.get("expect_dimension"),
                "expect_status_at_most": fault.get("expect_status_at_most"),
                "expect_completion_score_at_most": fault.get("expect_completion_score_at_most"),
                "actual_status": evaluation["actual_status"],
                "actual_completion_score": evaluation["actual_completion_score"],
            }
            (missed_faults if evaluation["outcome"] == "missed" else unverifiable_faults).append(row)

    recall = totals["caught"] / float(totals["faults"]) if totals["faults"] else None
    return {
        "dir": str(fi_dir),
        "totals": totals,
        "recall": recall,
        "missed_faults": missed_faults,
        "unverifiable_faults": unverifiable_faults,
        "skipped_samples": skipped,
        "non_reviewed_samples": non_reviewed,
    }


def render_markdown(result: dict) -> str:
    totals = result["totals"]
    recall = "%d/%d" % (totals["caught"], totals["faults"])
    lines = [
        "# Fault injection recall: %s" % result["dir"],
        "",
        "- samples: %(samples)d | reviewed: %(reviewed)d | skipped: %(skipped)d" % totals,
        "- recall: %s (caught %d / missed %d / unverifiable %d)"
        % (recall, totals["caught"], totals["missed"], totals["unverifiable"]),
        "",
    ]
    if result["missed_faults"]:
        lines.append("## Missed faults")
        lines.append("")
        for row in result["missed_faults"]:
            lines.append(
                "- %s / %s: dimension=%s expect_status_at_most=%s expect_completion_score_at_most=%s "
                "actual_status=%s actual_completion_score=%s"
                % (
                    row["sample_id"],
                    row["fault_id"],
                    row["dimension"],
                    row["expect_status_at_most"],
                    row["expect_completion_score_at_most"],
                    row["actual_status"],
                    row["actual_completion_score"],
                )
            )
        lines.append("")
    if result["unverifiable_faults"]:
        lines.append("## Unverifiable faults (null completion_score)")
        lines.append("")
        for row in result["unverifiable_faults"]:
            lines.append("- %s / %s: dimension=%s" % (row["sample_id"], row["fault_id"], row["dimension"]))
        lines.append("")
    if result["non_reviewed_samples"]:
        lines.append("## Non-reviewed samples (skipped)")
        lines.append("")
        lines += ["- %s" % sample_id for sample_id in result["non_reviewed_samples"]]
        lines.append("")
    if result["skipped_samples"]:
        lines.append("## Skipped samples")
        lines.append("")
        lines += ["- %s: %s" % (row["sample_id"], row["reason"]) for row in result["skipped_samples"]]
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="evals/fault-injection", help="fault-injection sample directory")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = run_fault_injection(args.dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
