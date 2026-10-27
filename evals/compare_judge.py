"""Compare a judge report against a reviewed regression sample (baseline).

Usage:

    python3 evals/compare_judge.py \
        --sample evals/judge-samples/<sample>.json \
        --run runs/<run-dir> [--json]

    # Aggregate over a whole gold-set directory (jrs-*.json):
    python3 evals/compare_judge.py \
        --samples-dir evals/judge-samples [--json] [--fail-below 0.8]

Dimension keys and statuses follow auditrail/agents/schemas.py
(DIMENSION_DEFINITIONS / ALLOWED_DIMENSION_STATUS). Agreement classes:

- agree: judge status == baseline status
- false_positive: judge pass, baseline partial/fail
- false_negative: judge fail, baseline pass/partial
- disagree: other status mismatch
- judge_missing / baseline_missing: one side has no label for the key

Samples with review_status != "reviewed" produce output clearly marked
PROVISIONAL: pre-labels are not a benchmark until human review.

Aggregate mode discovers ``jrs-*.json`` under --samples-dir and locates each
sample's ``agent_judge.json`` via ``run_ref.run_dir``. Aggregate metrics only
count ``reviewed`` samples; provisional samples are listed separately, and
samples whose run directory or judge report is missing count as ``skipped``
without aborting the run. ``--fail-below <0..1>`` exits 2 when any reviewed
dimension's agreement falls below the threshold (CI gate; disabled by
default).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auditrail.agents.schemas import ALLOWED_DIMENSION_STATUS, DIMENSION_DEFINITIONS

AGREE = "agree"
FALSE_POSITIVE = "false_positive"
FALSE_NEGATIVE = "false_negative"
DISAGREE = "disagree"
JUDGE_MISSING = "judge_missing"
BASELINE_MISSING = "baseline_missing"


def _load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _judge_dimensions(report: dict) -> dict:
    dims = report.get("dimensions")
    out = {}
    if isinstance(dims, list):
        for item in dims:
            if isinstance(item, dict) and item.get("key"):
                out[str(item["key"])] = item
    elif isinstance(dims, dict):
        for key, item in dims.items():
            if isinstance(item, dict):
                out[str(key)] = item
            else:
                out[str(key)] = {"status": str(item)}
    return out


def classify(baseline_status: str | None, judge_status: str | None) -> str:
    if baseline_status is None:
        return JUDGE_MISSING if judge_status is None else BASELINE_MISSING
    if judge_status is None:
        return JUDGE_MISSING
    if baseline_status == judge_status:
        return AGREE
    if judge_status == "pass" and baseline_status in ("partial", "fail"):
        return FALSE_POSITIVE
    if judge_status == "fail" and baseline_status in ("pass", "partial"):
        return FALSE_NEGATIVE
    return DISAGREE


def compare(sample: dict, report: dict) -> dict:
    baseline_dims = {d["key"]: d for d in sample["labels"]["dimensions"]}
    judge_dims = _judge_dimensions(report)
    rows = []
    counts = {}
    for key in DIMENSION_DEFINITIONS:
        baseline = baseline_dims.get(key, {}).get("status")
        judge = judge_dims.get(key, {}).get("status")
        if baseline is not None and baseline not in ALLOWED_DIMENSION_STATUS:
            raise ValueError("baseline status %r not allowed for %s" % (baseline, key))
        outcome = classify(baseline, judge)
        counts[outcome] = counts.get(outcome, 0) + 1
        rows.append(
            {
                "key": key,
                "label": DIMENSION_DEFINITIONS[key],
                "baseline": baseline,
                "judge": judge,
                "outcome": outcome,
            }
        )
    return {
        "sample_id": sample.get("sample_id"),
        "review_status": sample.get("labeling", {}).get("review_status"),
        "judge_overall": report.get("overall_status"),
        "baseline_overall": sample["labels"].get("overall_status"),
        "rows": rows,
        "counts": counts,
        "agreement": counts.get(AGREE, 0) / float(len(rows)),
    }


def render_markdown(result: dict) -> str:
    lines = [
        "# Judge vs baseline comparison: %s" % result["sample_id"],
        "",
    ]
    if result.get("review_status") != "reviewed":
        lines.append("> **PROVISIONAL**: baseline sample is `%s`, not human-reviewed." % result.get("review_status"))
        lines.append("")
    lines += [
        "- baseline overall: `%s`  | judge overall: `%s`" % (result["baseline_overall"], result["judge_overall"]),
        "- dimension agreement: %d/%d" % (result["counts"].get(AGREE, 0), len(result["rows"])),
        "",
        "| 维度 | 基准 | 裁判 | 结论 |",
        "|---|---|---|---|",
    ]
    for row in result["rows"]:
        lines.append(
            "| %s（%s） | %s | %s | %s |"
            % (row["key"], row["label"], row["baseline"] or "—", row["judge"] or "—", row["outcome"])
        )
    return "\n".join(lines)


OUTCOMES = (AGREE, FALSE_POSITIVE, FALSE_NEGATIVE, DISAGREE, JUDGE_MISSING, BASELINE_MISSING)


def _resolve_run_dir(run_dir: str) -> Path:
    """Resolve ``run_ref.run_dir`` (usually ``runs/<id>``, repo-relative)."""
    path = Path(run_dir).expanduser()
    if path.is_absolute() or path.exists():
        return path
    repo_relative = Path(__file__).resolve().parent.parent / path
    return repo_relative


def aggregate_samples(samples_dir: Path) -> dict:
    """Aggregate judge-vs-baseline outcomes over every ``jrs-*.json`` sample.

    Only ``reviewed`` samples feed the metrics; provisional samples are listed
    separately and samples with a missing run directory or judge report count
    as ``skipped`` (listed, never aborting the aggregation).
    """
    samples_dir = Path(samples_dir)
    totals = {"samples": 0, "reviewed": 0, "provisional": 0, "skipped": 0}
    provisional = []
    skipped = []
    dimensions = {
        key: {"counts": {outcome: 0 for outcome in OUTCOMES}, "total": 0, "agreement": None}
        for key in DIMENSION_DEFINITIONS
    }
    overall = {"agree": 0, "total": 0, "agreement": None}
    per_sample = []

    for path in sorted(samples_dir.glob("jrs-*.json")):
        totals["samples"] += 1
        try:
            sample = _load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            totals["skipped"] += 1
            skipped.append({"sample_id": path.stem, "reason": "unreadable sample: %s" % exc})
            continue
        sample_id = sample.get("sample_id") or path.stem
        review_status = sample.get("labeling", {}).get("review_status")
        if review_status != "reviewed":
            totals["provisional"] += 1
            provisional.append(sample_id)
            continue
        totals["reviewed"] += 1
        run_ref = sample.get("run_ref") or {}
        run_dir = _resolve_run_dir(str(run_ref.get("run_dir") or ""))
        if not run_dir.is_dir():
            totals["skipped"] += 1
            skipped.append({"sample_id": sample_id, "reason": "run directory not found: %s" % run_dir})
            continue
        report_path = run_dir / "agent_judge.json"
        if not report_path.exists():
            totals["skipped"] += 1
            skipped.append({"sample_id": sample_id, "reason": "missing %s" % report_path})
            continue
        try:
            result = compare(sample, _load_json(report_path))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            totals["skipped"] += 1
            skipped.append({"sample_id": sample_id, "reason": "uncomparable sample: %s" % exc})
            continue
        per_sample.append(result)
        for row in result["rows"]:
            bucket = dimensions[row["key"]]
            bucket["counts"][row["outcome"]] += 1
            bucket["total"] += 1
        overall["total"] += 1
        if result["judge_overall"] == result["baseline_overall"]:
            overall["agree"] += 1

    for bucket in dimensions.values():
        if bucket["total"]:
            bucket["agreement"] = bucket["counts"][AGREE] / float(bucket["total"])
    if overall["total"]:
        overall["agreement"] = overall["agree"] / float(overall["total"])
    return {
        "samples_dir": str(samples_dir),
        "totals": totals,
        "provisional_samples": provisional,
        "skipped_samples": skipped,
        "dimensions": dimensions,
        "overall_status": overall,
        "samples": per_sample,
    }


def render_aggregate_markdown(result: dict) -> str:
    totals = result["totals"]
    lines = [
        "# Judge vs baseline aggregate: %s" % result["samples_dir"],
        "",
        "- samples: %(samples)d | reviewed: %(reviewed)d | provisional: %(provisional)d | skipped: %(skipped)d"
        % totals,
    ]
    overall = result["overall_status"]
    if overall["total"]:
        lines.append(
            "- overall_status agreement: %d/%d (%.1f%%)"
            % (overall["agree"], overall["total"], overall["agreement"] * 100)
        )
    else:
        lines.append("- overall_status agreement: no reviewed samples")
    lines += [
        "",
        "| 维度 | agree | false_positive | false_negative | disagree | judge_missing | baseline_missing | 一致率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, bucket in result["dimensions"].items():
        counts = bucket["counts"]
        agreement = "%.1f%%" % (bucket["agreement"] * 100) if bucket["agreement"] is not None else "—"
        lines.append(
            "| %s（%s） | %d | %d | %d | %d | %d | %d | %s |"
            % (
                key,
                DIMENSION_DEFINITIONS[key],
                counts[AGREE],
                counts[FALSE_POSITIVE],
                counts[FALSE_NEGATIVE],
                counts[DISAGREE],
                counts[JUDGE_MISSING],
                counts[BASELINE_MISSING],
                agreement,
            )
        )
    if result["provisional_samples"]:
        lines += ["", "## Provisional samples (not counted)", ""]
        lines += ["- %s" % sample_id for sample_id in result["provisional_samples"]]
    if result["skipped_samples"]:
        lines += ["", "## Skipped samples", ""]
        lines += ["- %s: %s" % (row["sample_id"], row["reason"]) for row in result["skipped_samples"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample")
    parser.add_argument("--run")
    parser.add_argument("--samples-dir")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--fail-below",
        type=float,
        default=None,
        metavar="0..1",
        help="aggregate mode: exit 2 when any reviewed dimension agreement falls below this threshold",
    )
    args = parser.parse_args(argv)

    if args.samples_dir:
        result = aggregate_samples(Path(args.samples_dir))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(render_aggregate_markdown(result))
        if args.fail_below is not None:
            for key, bucket in result["dimensions"].items():
                if bucket["agreement"] is not None and bucket["agreement"] < args.fail_below:
                    print(
                        "fail-below: dimension %s agreement %.3f < %.3f" % (key, bucket["agreement"], args.fail_below),
                        file=sys.stderr,
                    )
                    return 2
        return 0

    if not args.sample or not args.run:
        parser.error("--sample and --run are required unless --samples-dir is given")
    sample = _load_json(Path(args.sample))
    report = _load_json(Path(args.run) / "agent_judge.json")
    result = compare(sample, report)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
