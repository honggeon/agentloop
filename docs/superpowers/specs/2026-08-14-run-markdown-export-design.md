# Run Markdown Export Design

## Goal

Add a Markdown export for a single evaluation run so users can download the current evaluation result as a portable report.

## Scope

The export applies to one run at a time from the run detail page. It includes the same categories already shown by the page: run summary, runtime health metrics, LLM metrics, tool metrics, deterministic findings, and DeepAgent judge output when present.

The export must still work when `agent_judge.json` is missing. In that case, the report states that DeepAgent analysis has not been generated and includes available summary, audit, charts, and runtime metrics.

## Architecture

The backend exposes `GET /api/runs/{run_id}/export.md`. It resolves the run path through the existing safe `resolve_run_path` flow, loads the existing JSON artifacts, derives runtime metrics with `_build_runtime_metrics`, renders deterministic Markdown, and returns it as `text/markdown` with a download filename.

The frontend adds a `导出 Markdown` button to `RunView.vue`. The button calls the new endpoint, receives a blob, and triggers a browser download named `<run_id>-evaluation.md`.

## Error Handling

Invalid or missing run IDs reuse the existing 400/404 behavior from `_run_path_or_404`. Missing optional artifacts render empty or explicit fallback sections instead of failing.

## Testing

Backend tests verify the endpoint returns Markdown with the expected content type, filename, run metadata, runtime metrics, judge summary, and findings. Frontend source tests verify the run page exposes the export action and endpoint.
