from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict

from .audit import audit_messages, render_markdown_report
from .client import AgentClient
from .trajectory import (
    llm_calls_from_session_response,
    messages_from_session_response,
    normalize_messages,
    session_summary,
    write_json,
    write_jsonl,
    write_text,
)
from .runtime import build_agent_judge_prompt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="auditrail")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch-session", help="Fetch an existing session and export trajectory files.")
    _add_common(fetch)
    fetch.add_argument("--session-id", required=True)
    fetch.add_argument("--out", default="runs")

    smoke = subparsers.add_parser("smoke-chat", help="Create a session, send a message, consume SSE, and export results.")
    _add_common(smoke)
    smoke.add_argument("--message", required=True)
    smoke.add_argument("--model", default="")
    smoke.add_argument("--max-turns", type=int, default=20)
    smoke.add_argument("--out", default="runs")

    render = subparsers.add_parser("render-run", help="Rebuild trajectory and audit files from an existing run/session.json.")
    render.add_argument("--run", required=True)

    judge = subparsers.add_parser("judge-run", help="Run DeepAgent analysis for an exported run directory.")
    judge.add_argument("--run", required=True)
    judge.add_argument("--thread-id", default=None)
    judge.add_argument("--output-format", choices=["json", "markdown", "both"], default="both")

    proxy = subparsers.add_parser("metrics-proxy", help="Run an OpenAI-compatible LLM metrics proxy.")
    proxy.add_argument("--upstream-base-url", required=True)
    proxy.add_argument("--host", default="127.0.0.1")
    proxy.add_argument("--port", type=int, default=7600)
    proxy.add_argument("--log", default="runs/proxy-llm-calls.jsonl")
    proxy.add_argument("--timeout", type=float, default=300.0)

    import_calls = subparsers.add_parser("import-llm-calls", help="Import proxy JSONL records into a run directory.")
    import_calls.add_argument("--run", required=True)
    import_calls.add_argument("--log", required=True)

    list_dsh = subparsers.add_parser("list-dsh-sessions", help="List locally recorded DeepSeek Harness sessions.")
    list_dsh.add_argument("--dsh-home", default=None)
    list_dsh.add_argument("--limit", type=int, default=20)
    list_dsh.add_argument("--json", action="store_true", help="Print JSON instead of a table.")

    import_dsh = subparsers.add_parser("import-dsh-session", help="Convert a recorded DSH session into a run directory.")
    import_dsh.add_argument("--session-id", default=None, help="DSH session id (session-<uuid>).")
    import_dsh.add_argument("--workspace", default=None, help="DSH workspace dir name, to disambiguate duplicate ids.")
    import_dsh.add_argument("--path", default=None, help="Direct path to a session.jsonl.zstd file.")
    import_dsh.add_argument("--dsh-home", default=None)
    import_dsh.add_argument("--out", default="runs")

    run_cases = subparsers.add_parser("run-cases", help="Run a deterministic acceptance-case suite against a target binary.")
    run_cases.add_argument("--suite", required=True, help="Path to a suite JSON file, e.g. cases/dsh-cli.json.")
    run_cases.add_argument("--bin", default=None, help="Target binary; defaults to the suite's binary field.")
    run_cases.add_argument("--only", action="append", default=None, help="Run only cases matching this id or module (repeatable).")
    run_cases.add_argument("--out", default="cases/results", help="Directory for the JSON and Markdown reports.")
    run_cases.add_argument("--no-write", action="store_true", help="Print results without writing report files.")
    run_cases.add_argument("--json", action="store_true", help="Print the full JSON report instead of the summary.")

    # ------------------------------------------------------------------
    # Phase 5: database schema + platform data migration + backup/restore
    # ------------------------------------------------------------------

    migrate_db = subparsers.add_parser(
        "migrate-db", help="Apply pending schema migrations (idempotent; never runs implicitly at API startup)."
    )
    _add_db_args(migrate_db)
    migrate_db.add_argument("--status", action="store_true", help="Only print migration status.")

    migrate_platform = subparsers.add_parser(
        "migrate-platform", help="Migrate platform metadata from files into the database (dry-run by default)."
    )
    _add_db_args(migrate_platform)
    migrate_platform.add_argument("--source", default="file", choices=["file"])
    migrate_platform.add_argument("--target", default="database", choices=["database"])
    migrate_platform.add_argument("--dry-run", action="store_true", default=None, help="Preview only (default).")
    migrate_platform.add_argument("--yes", action="store_true", help="Actually write (disables dry-run).")

    verify_migration = subparsers.add_parser(
        "verify-platform-migration", help="Compare file-side and database-side entity counts."
    )
    _add_db_args(verify_migration)

    rollback = subparsers.add_parser(
        "rollback-platform-migration",
        help="Delete only the rows one migration imported (dry-run by default; never touches files or runs).",
    )
    _add_db_args(rollback)
    rollback.add_argument("--migration-id", default=None, help="Defaults to the latest completed migration.")
    rollback.add_argument("--dry-run", action="store_true", default=None)
    rollback.add_argument("--yes", action="store_true", help="Actually delete (disables dry-run).")

    backup = subparsers.add_parser("backup-platform", help="Backup platform files + database (no secrets, no run evidence).")
    _add_db_args(backup, db_required=False)
    backup.add_argument("--output", required=True, help="Backup output directory.")
    backup.add_argument("--include-database", action="store_true", help="Also export database tables as JSONL.")

    verify_backup = subparsers.add_parser("verify-platform-backup", help="Verify a backup manifest and checksums.")
    verify_backup.add_argument("--backup", required=True, help="Backup directory (contains manifest.json).")

    restore = subparsers.add_parser(
        "restore-platform", help="Restore a backup (dry-run by default; never overwrites without --force)."
    )
    _add_db_args(restore, db_required=False)
    restore.add_argument("--backup", required=True)
    restore.add_argument("--restore-database", action="store_true", help="Also restore database tables.")
    restore.add_argument("--force", action="store_true", help="Allow overlaying a non-empty platform directory.")
    restore.add_argument("--dry-run", action="store_true", default=None)
    restore.add_argument("--yes", action="store_true", help="Actually restore (disables dry-run).")

    args = parser.parse_args(argv)
    if args.command == "fetch-session":
        return _fetch_session(args)
    if args.command == "smoke-chat":
        return _smoke_chat(args)
    if args.command == "render-run":
        return _render_run(args)
    if args.command == "judge-run":
        return _judge_run(args)
    if args.command == "metrics-proxy":
        return _metrics_proxy(args)
    if args.command == "import-llm-calls":
        return _import_llm_calls(args)
    if args.command == "list-dsh-sessions":
        return _list_dsh_sessions(args)
    if args.command == "import-dsh-session":
        return _import_dsh_session(args)
    if args.command == "run-cases":
        return _run_cases(args)
    if args.command == "migrate-db":
        return _migrate_db(args)
    if args.command == "migrate-platform":
        return _migrate_platform(args)
    if args.command == "verify-platform-migration":
        return _verify_platform_migration(args)
    if args.command == "rollback-platform-migration":
        return _rollback_platform_migration(args)
    if args.command == "backup-platform":
        return _backup_platform(args)
    if args.command == "verify-platform-backup":
        return _verify_platform_backup(args)
    if args.command == "restore-platform":
        return _restore_platform(args)
    parser.error("unknown command")
    return 2


# ---------------------------------------------------------------------------
# Phase 5 platform administration commands
# ---------------------------------------------------------------------------


def _add_db_args(parser: argparse.ArgumentParser, *, db_required: bool = True) -> None:
    parser.add_argument(
        "--platform-dir",
        default=None,
        help="Platform data directory (default: AUDITRAIL_PLATFORM_DIR or ./platform).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="sqlite:///path or postgresql://... (default: AUDITRAIL_DATABASE_URL or sqlite under --platform-dir).",
    )


def _platform_dir_arg(args) -> Path:
    import os

    raw = args.platform_dir or os.getenv("AUDITRAIL_PLATFORM_DIR") or "platform"
    return Path(raw).expanduser()


def _database_url_arg(args) -> str:
    import os

    url = args.database_url or os.getenv("AUDITRAIL_DATABASE_URL") or ""
    if not url:
        url = "sqlite:///%s" % (_platform_dir_arg(args) / "auditrail.db")
    return url


def _open_database(args):
    from .db import Database

    db = Database(_database_url_arg(args))
    return db


def _require_migrated(db) -> None:
    """Data operations need the schema; the API/CLI never auto-migrates."""
    from .db import MigrationError, migration_status

    status = migration_status(db)
    if not status.get("up_to_date"):
        raise SystemExit(
            "数据库 schema 未就绪（pending=%s）。请先运行 `auditrail migrate-db`。" % status.get("pending")
        )


def _migrate_db(args) -> int:
    from .db import apply_migrations, migration_status

    db = _open_database(args)
    if args.status:
        print(json.dumps(migration_status(db), ensure_ascii=False, indent=2))
        return 0
    applied = apply_migrations(db)
    status = migration_status(db)
    print(
        "migrate-db: applied=%s current_version=%d up_to_date=%s"
        % (applied or "[]", status["current_version"], status["up_to_date"])
    )
    return 0


def _migrate_platform(args) -> int:
    from . import platform_migrate

    dry_run = not args.yes
    db = _open_database(args)
    _require_migrated(db)
    report = platform_migrate.migrate_platform(_platform_dir_arg(args), db, dry_run=dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if dry_run:
        print("dry-run：未写入任何数据。加 --yes 执行真实迁移。")
    return 0


def _verify_platform_migration(args) -> int:
    from . import platform_migrate

    db = _open_database(args)
    _require_migrated(db)
    report = platform_migrate.verify_platform_migration(_platform_dir_arg(args), db)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["consistent"] else 1


def _rollback_platform_migration(args) -> int:
    from . import platform_migrate

    dry_run = not args.yes
    db = _open_database(args)
    _require_migrated(db)
    migration_id = args.migration_id or platform_migrate.latest_migration_id(db)
    if not migration_id:
        print("没有可回滚的已完成迁移。")
        return 1
    report = platform_migrate.rollback_platform_migration(db, migration_id, dry_run=dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if dry_run:
        print("dry-run：未删除任何数据。加 --yes 执行真实回滚。")
    return 0


def _backup_platform(args) -> int:
    from . import platform_backup

    db = None
    if args.include_database:
        db = _open_database(args)
        _require_migrated(db)
    manifest = platform_backup.backup_platform(_platform_dir_arg(args), Path(args.output), db=db)
    print(
        "backup %s created: files=%d tables=%d"
        % (
            manifest["backup_id"],
            manifest["components"].get("platform_files", {}).get("file_count", 0),
            manifest["components"].get("database", {}).get("table_count", 0),
        )
    )
    return 0


def _verify_platform_backup(args) -> int:
    from . import platform_backup

    result = platform_backup.verify_platform_backup(Path(args.backup))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


def _restore_platform(args) -> int:
    from . import platform_backup

    dry_run = not args.yes
    db = None
    if args.restore_database:
        db = _open_database(args)
        _require_migrated(db)
    report = platform_backup.restore_platform(
        Path(args.backup), _platform_dir_arg(args), db=db, dry_run=dry_run, force=bool(args.force)
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if dry_run:
        print("dry-run：未恢复任何数据。加 --yes 执行真实恢复。")
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--token", default=None)
    parser.add_argument("--timeout", type=float, default=60.0)


def _fetch_session(args) -> int:
    client = AgentClient(args.base_url, args.user_id, token=args.token, timeout=args.timeout)
    session_response = client.fetch_session(args.session_id)
    run_dir = _run_dir(Path(args.out), args.session_id)
    _write_run_outputs(
        run_dir,
        session_response,
        source=_source_meta(args.base_url, args.user_id),
    )
    print("Exported session %s to %s" % (args.session_id, run_dir))
    return 0


def _smoke_chat(args) -> int:
    client = AgentClient(args.base_url, args.user_id, token=args.token, timeout=args.timeout)
    created = client.create_session(title="auditrail smoke test", model=args.model)
    session = created.get("session") or {}
    session_id = session.get("session_id")
    if not session_id:
        raise RuntimeError("session creation response did not include session.session_id")
    started = client.start_chat(session_id, args.message, model=args.model or None, max_turns=args.max_turns)
    stream_id = started.get("stream_id")
    if not stream_id:
        raise RuntimeError("chat start response did not include stream_id")
    for event_name, payload in client.stream_events(stream_id):
        print("sse %s %s" % (event_name, _compact(payload)))
        if event_name in {"stream_end", "error", "cancelled"}:
            break
    session_response = client.fetch_session(session_id)
    run_dir = _run_dir(Path(args.out), session_id)
    _write_run_outputs(
        run_dir,
        session_response,
        source=_source_meta(args.base_url, args.user_id),
    )
    print("Exported smoke chat %s to %s" % (session_id, run_dir))
    return 0


def _render_run(args) -> int:
    run_dir = Path(args.run)
    session_path = run_dir / "session.json"
    if not session_path.exists():
        session_id = run_dir.name
        if "-" in session_id and len(session_id) > 36:
            session_id = session_id[-36:]
        print("Missing %s" % session_path, file=sys.stderr)
        print("", file=sys.stderr)
        print("render-run only rebuilds reports from an existing run directory.", file=sys.stderr)
        print("First fetch the session, for example:", file=sys.stderr)
        print(
            "  python3 -m auditrail.cli fetch-session --base-url http://192.168.1.238:8788 --user-id auditrail-test --session-id %s"
            % session_id,
            file=sys.stderr,
        )
        return 2
    session_response = json.loads(session_path.read_text(encoding="utf-8"))
    _recover_dsh_model(session_response)
    _write_run_outputs(run_dir, session_response)
    print("Rendered run %s" % run_dir)
    return 0


def _recover_dsh_model(session_response: Dict) -> None:
    """Fill a missing session model from recorded DSH events, if present."""
    session = session_response.get("session")
    if not isinstance(session, dict) or session.get("model"):
        return
    events = session_response.get("dsh_events")
    if not isinstance(events, list) or not events:
        return
    from .dsh_source import _model_of

    model = _model_of(events)
    if model:
        session["model"] = model


def _judge_run(args) -> int:
    from .agents.schemas import resolve_judge_report
    from .agents.tools import write_judge_report
    from .judges.base import JudgeEngineError, JudgeRequest
    from .judges.deepagent_engine import DeepAgentJudgeEngine

    run_dir = Path(args.run)
    for filename in ("summary.json", "audit.json", "trajectory.jsonl"):
        path = run_dir / filename
        if not path.exists():
            print("Missing %s" % path, file=sys.stderr)
            return 2

    # 统一裁判执行边界：引擎负责候选优选与一次有界修复；write_judge_report
    # 是发布前最后一道完整性门禁。proposal 结构不完整时明确失败（返回 2），
    # 不写出或覆盖任何报告文件。
    engine = DeepAgentJudgeEngine()
    request = JudgeRequest(run_dir=run_dir, run_id=run_dir.name, thread_id=args.thread_id or "")
    try:
        result = engine.run(request)
        report = resolve_judge_report(result.proposal, run_dir)
        paths = write_judge_report(str(run_dir), report)
    except JudgeEngineError as exc:
        print("Judge failed: %s" % exc, file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as exc:
        print("Judge report rejected: %s" % exc, file=sys.stderr)
        return 2
    if args.output_format in {"json", "both"}:
        print("Wrote %s" % paths["json_path"])
    if args.output_format in {"markdown", "both"}:
        print("Wrote %s" % paths["markdown_path"])
    print("Wrote %s" % paths["chart_path"])
    return 0


def _metrics_proxy(args) -> int:
    print(
        "Starting metrics proxy http://%s:%s/v1 -> %s"
        % (args.host, args.port, args.upstream_base_url.rstrip("/")),
        file=sys.stderr,
    )
    print("Writing LLM call logs to %s" % args.log, file=sys.stderr)
    try:
        import uvicorn

        from .llm_proxy import create_proxy_app

        app = create_proxy_app(args.upstream_base_url, Path(args.log), timeout=args.timeout)
        uvicorn.run(app, host=args.host, port=args.port)
    except ModuleNotFoundError as exc:
        if exc.name not in {"uvicorn", "fastapi"}:
            raise
        from .llm_proxy import serve_stdlib_proxy

        print("Falling back to stdlib HTTP server because %s is not installed." % exc.name, file=sys.stderr)
        serve_stdlib_proxy(args.upstream_base_url, Path(args.log), args.host, args.port, timeout=args.timeout)
    return 0


def _import_llm_calls(args) -> int:
    from .llm_proxy import import_proxy_log

    payload = import_proxy_log(Path(args.log), Path(args.run))
    print("Imported %s LLM calls to %s" % (len(payload["llm_calls"]), Path(args.run) / "llm_calls.json"))
    return 0


def _list_dsh_sessions(args) -> int:
    from .dsh_source import default_dsh_home, list_sessions

    dsh_home = Path(args.dsh_home).expanduser() if args.dsh_home else default_dsh_home()
    sessions = list_sessions(dsh_home, limit=args.limit)
    if args.json:
        print(json.dumps({"dsh_home": str(dsh_home), "sessions": sessions}, ensure_ascii=False, indent=2))
        return 0
    if not sessions:
        print("No DSH session recordings under %s" % dsh_home)
        return 0
    for item in sessions:
        print(
            "%s  %-40s  msgs=%s tools=%s  %s"
            % (
                item.get("session_id") or "?",
                (item.get("title") or "(untitled)")[:40],
                item.get("user_message_count", 0),
                item.get("tool_call_count", 0),
                item.get("workspace") or item.get("workspace_dir") or "",
            )
        )
    return 0


def _import_dsh_session(args) -> int:
    from .dsh_source import (
        default_dsh_home,
        find_session_file,
        load_session_response,
        source_meta,
    )

    if args.path:
        session_path = Path(args.path).expanduser()
        if not session_path.is_file():
            print("Missing %s" % session_path, file=sys.stderr)
            return 2
    elif args.session_id:
        dsh_home = Path(args.dsh_home).expanduser() if args.dsh_home else default_dsh_home()
        try:
            session_path = find_session_file(dsh_home, args.session_id, args.workspace)
        except (ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        print("import-dsh-session requires --session-id or --path", file=sys.stderr)
        return 2

    session_response = load_session_response(session_path)
    session_id = session_response.get("session", {}).get("session_id") or session_path.parent.name
    short_id = session_id[8:] if session_id.startswith("session-") else session_id
    short_id = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in short_id) or "unknown"
    run_dir = _run_dir(Path(args.out), "dsh-%s" % short_id)
    _write_run_outputs(run_dir, session_response, source=source_meta(session_response))
    print("Imported DSH session %s to %s" % (session_id, run_dir))
    return 0


def _run_cases(args) -> int:
    from .cases import load_suite, run_suite, write_report

    try:
        suite = load_suite(args.suite)
        report = run_suite(suite, only=args.only, binary=args.bin)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render_case_summary(report))

    if not args.no_write:
        paths = write_report(report, args.out)
        print("Wrote %s" % paths["json_path"], file=sys.stderr)
        print("Wrote %s" % paths["markdown_path"], file=sys.stderr)
    return 0 if report["summary"]["ok"] else 1


def _render_case_summary(report: Dict) -> str:
    summary = report["summary"]
    lines = [
        "suite: %s  binary: %s" % (report.get("suite"), report.get("binary")),
        "target: %s" % (report.get("target") or ""),
        "",
    ]
    label = {"passed": "PASS", "failed": "FAIL", "xfail": "XFAIL", "xpass": "XPASS", "error": "ERROR"}
    for case in report.get("cases") or []:
        lines.append(
            "%-6s %-14s %s"
            % (label.get(case["verdict"], case["verdict"]), case["id"], case.get("title") or "")
        )
        if case["verdict"] in {"failed", "error", "xpass"}:
            for check in case["checks"]:
                if not check["ok"]:
                    lines.append(
                        "       %s: expected %s, got %s"
                        % (check["assertion"], check["expected"], check["actual"])
                    )
        if case.get("defect"):
            lines.append("       defect: %s" % case["defect"])
    lines.extend(
        [
            "",
            "%s cases: %s passed, %s failed, %s xfail, %s xpass, %s error — %s"
            % (
                summary["total"],
                summary["passed"],
                summary["failed"],
                summary["xfail"],
                summary["xpass"],
                summary["error"],
                "PASS" if summary["ok"] else "FAIL",
            ),
        ]
    )
    return "\n".join(lines)


def _write_run_outputs(
    run_dir: Path,
    session_response: Dict,
    source: Dict | None = None,
) -> None:
    messages = messages_from_session_response(session_response)
    normalized = normalize_messages(messages)
    summary = session_summary(session_response)
    llm_calls = llm_calls_from_session_response(session_response)
    audit = audit_messages(messages)
    source_meta = source or _load_existing_source(run_dir)
    if source_meta:
        summary.update(source_meta)
        write_json(run_dir / "source.json", source_meta)

    write_json(run_dir / "session.json", session_response)
    write_json(run_dir / "summary.json", summary)
    write_jsonl(run_dir / "trajectory.jsonl", normalized)
    write_json(run_dir / "audit.json", audit)
    if llm_calls:
        write_json(run_dir / "llm_calls.json", {"llm_calls": llm_calls})
    else:
        _import_configured_llm_calls(run_dir)
    write_text(run_dir / "tool_audit.md", render_markdown_report(summary, audit))


def _source_meta(base_url: str, user_id: str = "") -> Dict:
    from urllib.parse import urlparse

    cleaned = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    host = parsed.hostname or ""
    netloc = parsed.netloc or host
    return {
        "source_base_url": cleaned,
        "server_host": host or netloc or "unknown",
        "server_netloc": netloc,
        "source_user_id": str(user_id or "").strip(),
    }


def _load_existing_source(run_dir: Path) -> Dict | None:
    source_path = run_dir / "source.json"
    if source_path.exists():
        try:
            data = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("server_host"):
            return data
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(summary, dict):
        return None
    host = str(summary.get("server_host") or "").strip()
    base_url = str(summary.get("source_base_url") or "").strip()
    if not host and not base_url:
        return None
    return {
        "source_base_url": base_url,
        "server_host": host or "unknown",
        "server_netloc": str(summary.get("server_netloc") or ""),
        "source_user_id": str(summary.get("source_user_id") or ""),
    }


def _import_configured_llm_calls(run_dir: Path) -> None:
    from .llm_proxy import import_configured_proxy_log

    try:
        payload = import_configured_proxy_log(run_dir)
    except Exception as exc:
        print("Warning: failed to import configured LLM proxy log: %s" % exc, file=sys.stderr)
        return
    if payload is not None:
        print("Imported %s configured LLM calls to %s" % (len(payload["llm_calls"]), run_dir / "llm_calls.json"))


def _run_dir(root: Path, session_id: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return root / ("%s-%s" % (stamp, session_id))


def _available_run_dir(base: Path) -> Path:
    """Return ``base`` or the first free ``-N`` suffix; never overwrite a run."""
    if not base.exists():
        return base
    suffix = 2
    while True:
        candidate = base.with_name("%s-%s" % (base.name, suffix))
        if not candidate.exists():
            return candidate
        suffix += 1


def _compact(payload) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text[:300] + "..." if len(text) > 300 else text


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
