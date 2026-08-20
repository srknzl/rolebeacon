from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn

from .company import CompanyResearchService
from .config import Settings
from .database import Database
from .domain import time_ago
from .evaluation import run_model_evaluation, run_rules_evaluation
from .job_export import export_jobs
from .llm import LlmClient
from .migration import import_legacy
from .setup import LocalModelService, SetupService
from .sync import INTERACTIVE_KINDS, SyncService
from .wizard import SetupWizard

INTERACTIVE_SOURCE_WARNING = """
  Automated access to signed-in LinkedIn pages is against LinkedIn's User Agreement, and the
  account at risk is the one you apply with. A warning, a temporary restriction, or a permanent
  ban are all possible outcomes. The pacing keeps this walk human-paced; it does not hide it.

  Only job searches and job postings are read - never a profile, connection list, message, or
  the feed - and an application is never submitted for you.

  What happens next:
    1. A Chrome window opens on a profile of its own. RoleBeacon never sees, types, or stores a
       password; deleting that profile directory ends the session.
    2. If the session has expired the walk stops on LinkedIn's sign-in page and waits up to five
       minutes for you to sign in, verification step included. It continues by itself.
    3. Leave the window open and untouched. Progress is printed here, naming each posting read.
    4. Press Ctrl-C, or close the window, to stop. Everything collected so far is saved and
       scored, and the next run resumes from that point.

  Turn these sources off on the Sources page if you do not accept this.
"""


def _report_interactive_sources(settings: Settings) -> None:
    """State what a signed-in walk risks before one opens, the way the Sources page does.

    Printed rather than prompted: --interactive is itself the deliberate act, and a sync that
    blocks on a question cannot be run from a script.
    """
    enabled = [
        source for source in settings.load_sources()
        if source.enabled and source.kind in INTERACTIVE_KINDS
    ]
    if not enabled:
        # --interactive with nothing to run is silent otherwise, and the CLI has no way to enable
        # a source, so say where that is done.
        print("No signed-in sources are enabled; --interactive has no effect. Enable them on the "
              "Sources page of `rolebeacon serve`.", file=sys.stderr)
        return
    print(f"Signed-in collection is enabled for: {', '.join(source.name or source.id for source in enabled)}",
          file=sys.stderr)
    print(INTERACTIVE_SOURCE_WARNING, file=sys.stderr)


def _emit(as_json: bool, payload: Any, lines: list[str]) -> None:
    """One command, two audiences: a person reads the lines, a script reads the JSON."""
    print(json.dumps(payload, indent=2, ensure_ascii=False) if as_json else "\n".join(lines))


def _status_report(settings: Settings, database: Database) -> tuple[dict[str, Any], list[str]]:
    stats = database.dashboard_stats()
    states = {row["source_id"]: row for row in database.list_sources()}
    configured = settings.load_sources()
    tally = {"ok": 0, "error": 0, "never run": 0, "disabled": 0}
    attention: list[tuple[str, str]] = []
    for source in configured:
        state = states.get(source.id, {})
        if not source.enabled:
            tally["disabled"] += 1
            continue
        if state.get("status") == "error":
            tally["error"] += 1
            attention.append((source.name or source.id, f"error: {state.get('last_error') or 'unknown'}"))
        elif not state.get("last_successful_sync_at"):
            tally["never run"] += 1
            attention.append((source.name or source.id, "never run"))
        else:
            tally["ok"] += 1

    latest = max(
        (state for state in states.values() if state.get("last_successful_sync_at")),
        key=lambda state: str(state["last_successful_sync_at"]),
        default=None,
    )
    lines = [
        f"{stats['total']:,} active jobs · {stats['new_today']:,} new today · "
        f"{stats['shortlisted']:,} bookmarked · {stats['pending_llm']:,} waiting for a model",
        "",
        f"{len(configured):,} sources: " + " · ".join(f"{count} {label}" for label, count in tally.items() if count),
    ]
    if latest is not None:
        names = {source.id: source.name or source.id for source in configured}
        source_id = str(latest["source_id"])
        lines.append(
            f"Last refresh: {time_ago(str(latest['last_successful_sync_at']))} — "
            f"{names.get(source_id, source_id)} "
            f"({latest.get('jobs_seen') or 0} seen, {latest.get('last_jobs_new') or 0} new)"
        )
    if attention:
        width = max(len(name) for name, _ in attention)
        lines += ["", "Needs attention:"] + [f"  {name:<{width}}  {note}" for name, note in attention]
    return {"stats": stats, "sources": list(states.values())}, lines


def _evaluation_lines(report: dict[str, Any]) -> list[str]:
    summary = report.get("summary", {})
    failed = [name for name, ok in report.get("ranking_checks", {}).items() if not ok]
    lines = [
        f"{'PASS' if report.get('passed') else 'FAIL'} — "
        f"{summary.get('cases_passed', 0)} of {summary.get('cases_total', 0)} cases",
    ]
    if summary.get("median_latency_seconds"):
        lines.append(
            f"{summary.get('model_calls', 0)} model calls · median "
            f"{summary['median_latency_seconds']}s · max {summary.get('max_latency_seconds', 0)}s"
        )
    if failed:
        lines += ["", "Failed ranking checks:"] + [f"  - {name.replace('_', ' ')}" for name in failed]
    return lines


def _flat_lines(payload: dict[str, Any]) -> list[str]:
    """A dict as one "key: value" per line - a readable last resort, not a design.

    Used for the model and migration commands, whose payloads are already a short list of
    findings rather than a nested report.
    """
    lines = []
    for key, value in payload.items():
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value) or "none"
        if isinstance(value, bool):
            value = "yes" if value else "no"
        lines.append(f"{str(key).replace('_', ' ').capitalize()}: {value}")
    return lines


def _sync_lines(status: dict[str, Any]) -> list[str]:
    lines = [
        f"Refresh {status.get('phase') or 'idle'}: {status.get('phase_message') or ''}".rstrip(": "),
        f"{status.get('sources_completed') or 0} of {status.get('sources_total') or 0} sources · "
        f"{status.get('jobs_seen') or 0} jobs seen · {status.get('jobs_changed') or 0} changed · "
        f"{status.get('jobs_scored') or 0} scored",
    ]
    if status.get("source_errors"):
        lines.append(f"{status['source_errors']} source(s) failed; `rolebeacon status` names them")
    if status.get("rule_fallback_jobs"):
        lines.append(f"{status['rule_fallback_jobs']} job(s) fell back to rules scoring")
    if status.get("llm_error"):
        lines.append(f"Scoring engine: {status['llm_error']}")
    if status.get("error"):
        lines.append(f"Failed: {status['error']}")
    return lines


def _doctor_lines(checks: dict[str, Any]) -> list[str]:
    problems = []
    if not checks["resources_present"]:
        problems.append("packaged templates, static files, or config are missing from the installation")
    if not checks["data_directory_writable"]:
        problems.append(f"the data directory is not writable: {checks['data_directory']}")
    if not checks["setup_complete"]:
        problems.append("setup has not been completed; run `rolebeacon setup`")
    elif not checks["activated"]:
        problems.append("collection is not activated, so no source will be contacted")
    if not checks["rules_only_ready"]:
        problems.append("no saved candidate or search profile, so rules-only scoring cannot run")
    lines = ["Everything checks out." if not problems else "Problems found:"]
    lines += [f"  - {problem}" for problem in problems]
    lines += [
        "",
        f"Data directory: {checks['data_directory']}",
        f"Database: {checks['database']}",
        f"Scoring mode: {checks['llm_mode']}",
    ]
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(prog="rolebeacon")
    # Accepted before or after the subcommand. SUPPRESS on the subparser copy stops an absent
    # flag there from overwriting one given up front.
    json_help = "Print machine-readable JSON instead of the human summary"
    parser.add_argument("--json", dest="as_json", action="store_true", help=json_help)
    json_flag = argparse.ArgumentParser(add_help=False)
    json_flag.add_argument("--json", dest="as_json", action="store_true", default=argparse.SUPPRESS, help=json_help)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Run the local web application")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sync_command = subparsers.add_parser("sync", help="Run one incremental sync", parents=[json_flag])
    sync_command.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "Also run sources that open a browser window and may wait for you to sign in. "
            "Signed-in LinkedIn collection breaches LinkedIn's User Agreement; the run prints "
            "what that risks before the window opens"
        ),
    )
    sync_command.add_argument(
        "--force",
        action="store_true",
        help="Run every enabled source now, ignoring its minimum interval",
    )
    jobs = subparsers.add_parser("jobs", help="Refresh and export ranked job discovery results", parents=[json_flag])
    jobs.add_argument("--no-sync", action="store_true", help="Export the existing local database without refreshing")
    jobs.add_argument("--start-ollama", action="store_true", help="Start an installed Ollama before refreshing")
    jobs.add_argument("--from-json", type=Path, help="Import a complete SetupPayloadV1 before running")
    jobs.add_argument("--output-dir", type=Path, default=Path.cwd(), help="Parent directory for the timestamped export")
    subparsers.add_parser("status", help="Show source state and database statistics", parents=[json_flag])
    subparsers.add_parser("doctor", help="Check setup, storage, database, and model readiness", parents=[json_flag])
    setup = subparsers.add_parser("setup", help="Run the interactive wizard, or import SetupPayloadV1 JSON", parents=[json_flag])
    setup.add_argument("--from-json", type=Path, help="Import a SetupPayloadV1 document instead of asking questions")
    setup.add_argument("--activate", action="store_true", help="Explicitly activate collection after import")
    setup.add_argument(
        "--no-interactive",
        action="store_true",
        help="Refuse to start the interactive wizard; requires --from-json",
    )
    migrate = subparsers.add_parser("migrate", help="Copy data from a legacy Job Radar installation", parents=[json_flag])
    migrate.add_argument("--from", dest="legacy_root", type=Path, required=True)
    model = subparsers.add_parser("model", help="Manage an optional local model runtime", parents=[json_flag])
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_commands.add_parser("doctor", help="Detect Ollama and locally available models", parents=[json_flag])
    model_commands.add_parser("start", help="Start an installed Ollama service", parents=[json_flag])
    pull = model_commands.add_parser("pull", help="Explicitly download an Ollama model", parents=[json_flag])
    pull.add_argument("model", nargs="?", default="qwen3:8b")
    test = model_commands.add_parser("test", help="Test an OpenAI-compatible endpoint", parents=[json_flag])
    test.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    test.add_argument("--model", default="qwen3:8b")
    test.add_argument("--api-key", default="")
    evaluate = subparsers.add_parser("evaluate-model", help="Run the repeatable scoring-quality evaluation", parents=[json_flag])
    evaluate.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    evaluate.add_argument("--model", default="qwen3:14b")
    evaluate.add_argument("--api-key", default="")
    evaluate.add_argument("--provider", choices=("ollama", "custom"), default="ollama")
    evaluate.add_argument("--runs", type=int, default=1)
    evaluate.add_argument("--output", type=Path)
    evaluate_rules = subparsers.add_parser("evaluate-rules", help="Run the deterministic scoring-quality evaluation", parents=[json_flag])
    evaluate_rules.add_argument("--runs", type=int, default=3)
    evaluate_rules.add_argument("--output", type=Path)
    research = subparsers.add_parser("research-company", help="Refresh a provenance-backed company profile", parents=[json_flag])
    research.add_argument("company")
    args = parser.parse_args()

    if args.command == "jobs" and args.start_ollama and args.no_sync:
        parser.error("--start-ollama cannot be combined with --no-sync")
    settings = Settings.load()
    if args.command == "jobs" and args.from_json:
        try:
            payload = json.loads(args.from_json.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Setup JSON must be an object")
            setup_service = SetupService(settings)
            validation = setup_service.validate_setup_payload(payload)
            if not validation["valid"]:
                raise ValueError(json.dumps(validation["errors"], ensure_ascii=False))
            imported_mode = str(validation["payload"].get("llm", {}).get("mode") or "rules")
            if args.start_ollama and imported_mode != "ollama":
                raise ValueError("--start-ollama requires SetupPayloadV1 to select Ollama")
            if not args.no_sync and payload.get("activate") is not True:
                raise ValueError("SetupPayloadV1 must set activate to true when jobs refreshes sources")
            settings = setup_service.complete(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            parser.error(f"invalid --from-json SetupPayloadV1: {error}")
    if args.command == "jobs" and args.start_ollama:
        if settings.llm_mode != "ollama":
            parser.error("--start-ollama requires the saved scoring mode to be Ollama")
    if args.command in {"sync", "jobs"}:
        # Collectors report progress through logging; send it to stderr so the JSON these
        # commands print on stdout stays pipeable. "serve" uses the refresh panel instead.
        logging.basicConfig(
            level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr
        )
        # Only RoleBeacon's own progress, not every httpx request line.
        logging.getLogger("rolebeacon").setLevel(logging.INFO)
    # JSON stays the default off a terminal, so existing pipelines keep the output they parse.
    as_json = bool(getattr(args, "as_json", False)) or not sys.stdout.isatty()
    settings.ensure_directories()
    if args.command == "migrate":
        migrated = import_legacy(settings, args.legacy_root)
        _emit(as_json, migrated, _flat_lines(migrated))
        return
    if args.command == "setup":
        if args.from_json is None:
            if args.no_interactive:
                parser.error("--no-interactive requires --from-json PATH")
            if not sys.stdin.isatty():
                parser.error("setup needs a terminal; pipe a document with --from-json PATH instead")
            summary = SetupWizard(settings).run()
            if summary is None:
                raise SystemExit(1)
            _emit(as_json, summary, _flat_lines(summary))
            return
        payload = json.loads(args.from_json.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("Setup JSON must be an object")
        payload["activate"] = bool(args.activate)
        service = SetupService(settings)
        validation = service.validate_setup_payload(payload)
        if not validation["valid"]:
            raise SystemExit(json.dumps({"errors": validation["errors"]}, indent=2, ensure_ascii=False))
        saved = service.complete(payload)
        enabled_ids = [source.id for source in saved.load_sources() if source.enabled]
        _emit(
            as_json,
            {
                "setup_complete": saved.setup_complete,
                "activated": saved.activated,
                "enabled_source_ids": enabled_ids,
                "scoring_mode": saved.llm_mode,
            },
            [
                f"Setup saved. Collection is {'activated' if saved.activated else 'not activated'}.",
                f"{len(enabled_ids)} source(s) enabled · scoring mode: {saved.llm_mode}",
            ],
        )
        return
    if args.command == "serve":
        # Resolve overrides once so uvicorn's listener and the app's local-origin allowlist use
        # exactly the same host and port.
        settings = replace(settings, host=args.host or settings.host, port=args.port or settings.port)

    database = Database(settings.database_path)
    database.initialize()

    if args.command == "serve":
        from .app import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    elif args.command == "sync":
        sync_service = SyncService(settings, database, LlmClient(settings))
        if args.interactive:
            _report_interactive_sources(settings)
        sync_result = asyncio.run(sync_service.run(force=args.force, manual=args.interactive))
        _emit(as_json, sync_result.to_dict(), _sync_lines(sync_result.to_dict()))
    elif args.command == "jobs":
        exit_code = _run_jobs_command(args, settings, database, as_json=as_json)
        if exit_code:
            raise SystemExit(exit_code)
    elif args.command == "status":
        payload, lines = _status_report(settings, database)
        _emit(as_json, payload, lines)
    elif args.command == "research-company":
        research_service = CompanyResearchService(settings, database, LlmClient(settings))
        company_id = asyncio.run(research_service.research(args.company))
        company = database.get_company(company_id) or {}
        researched = {
            "id": company.get("id"),
            "name": company.get("name"),
            "score": company.get("score"),
            "confidence": company.get("confidence"),
            "remote_policy": company.get("remote_policy"),
            "sponsorship": company.get("sponsorship"),
            "relocation": company.get("relocation"),
            "evidence_count": len(company.get("evidence", [])),
        }
        _emit(
            as_json,
            researched,
            [
                f"{researched['name'] or args.company}: company fit {researched['score'] or '—'} "
                f"(confidence {researched['confidence'] or '—'}) from {researched['evidence_count']} "
                "fetched official page(s)",
                f"Remote policy: {researched['remote_policy'] or 'unknown'} · "
                f"sponsorship: {researched['sponsorship'] or 'unknown'} · "
                f"relocation: {researched['relocation'] or 'unknown'}",
            ],
        )
    elif args.command == "doctor":
        checks = {
            "setup_complete": settings.setup_complete,
            "activated": settings.activated,
            "data_directory": str(settings.data_dir),
            "data_directory_writable": os.access(settings.data_dir, os.W_OK),
            "database": str(settings.database_path),
            "resources_present": all(
                (settings.resource_dir / item).exists() for item in ("templates", "static", "config")
            ),
            "rules_only_ready": settings.candidate_profile_path.exists() and settings.search_profile_path.exists(),
            "llm_mode": settings.llm_mode,
        }
        _emit(as_json, checks, _doctor_lines(checks))
        if not checks["resources_present"] or not checks["data_directory_writable"]:
            raise SystemExit(1)
    elif args.command == "model":
        models = LocalModelService(settings)
        if args.model_command == "doctor":
            model_result = asyncio.run(models.discover())
        elif args.model_command == "start":
            model_result = models.start_ollama()
        elif args.model_command == "pull":
            model_result = asyncio.run(models.pull_ollama_model(args.model))
        else:
            model_result = asyncio.run(
                models.test_endpoint(base_url=args.base_url, model=args.model, api_key=args.api_key)
            )
        _emit(as_json, model_result, _flat_lines(model_result))
    elif args.command == "evaluate-model":
        evaluation_settings = replace(
            settings,
            llm_mode=args.provider,
            llm_enabled=True,
            llm_base_url=args.base_url.rstrip("/"),
            llm_model=args.model,
            llm_api_key=args.api_key,
        )
        report = asyncio.run(run_model_evaluation(LlmClient(evaluation_settings), runs=max(1, args.runs)))
        full_report = {"model": args.model, "base_url": args.base_url, **report}
        if args.output:
            # The file is always the complete report: it is the artifact the run exists to leave
            # behind, whatever the terminal chose to show.
            args.output.write_text(f"{json.dumps(full_report, indent=2)}\n", encoding="utf-8")
        _emit(as_json, full_report, [f"{args.model} at {args.base_url}", *_evaluation_lines(report)])
        if not report["passed"]:
            raise SystemExit(1)
    elif args.command == "evaluate-rules":
        rules_report = run_rules_evaluation(runs=max(2, args.runs))
        if args.output:
            args.output.write_text(f"{json.dumps(rules_report, indent=2)}\n", encoding="utf-8")
        _emit(
            as_json,
            rules_report,
            [str(rules_report.get("engine", "rules")), *_evaluation_lines(rules_report)],
        )
        if not rules_report["passed"]:
            raise SystemExit(1)


async def _ensure_ollama_ready(
    settings: Settings,
    *,
    timeout_seconds: float = 30,
    poll_interval_seconds: float = 1,
) -> dict[str, Any]:
    endpoint = urlsplit(settings.llm_base_url)
    hostname = endpoint.hostname or ""
    loopback = hostname == "localhost" or hostname.endswith(".localhost")
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
    if not loopback:
        raise RuntimeError(
            "--start-ollama can manage only a loopback endpoint such as "
            "http://127.0.0.1:11434/v1; start a configured LAN Ollama on its own host"
        )
    if endpoint.scheme != "http":
        raise RuntimeError("--start-ollama requires an HTTP loopback endpoint")

    try:
        port = endpoint.port or 80
    except ValueError as error:
        raise RuntimeError(f"--start-ollama received an invalid endpoint port: {error}") from error
    bind_hostname = f"[{hostname}]" if ":" in hostname else hostname
    ollama_host = f"{bind_hostname}:{port}"

    llm = LlmClient(settings)
    health = await llm.health()
    if health["available"]:
        return {"started": False, "health": health}

    started = LocalModelService(settings).start_ollama(host=ollama_host)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        health = await llm.health()
        if health["available"]:
            return {"started": True, "process": started, "health": health}
        remaining = deadline - loop.time()
        if remaining <= 0:
            detail = str(health.get("error") or "configured model did not become available")
            raise RuntimeError(f"Ollama did not become ready within {timeout_seconds:g} seconds: {detail}")
        await asyncio.sleep(min(poll_interval_seconds, remaining))


def _run_jobs_command(
    args: argparse.Namespace, settings: Settings, database: Database, *, as_json: bool = False
) -> int:
    sync_requested = not args.no_sync
    sync_performed = False
    status: dict[str, Any] | None = None
    fatal_error = ""

    if sync_requested and args.start_ollama:
        try:
            asyncio.run(_ensure_ollama_ready(settings))
        except Exception as error:
            fatal_error = f"{type(error).__name__}: {error}"
            status = {"phase": "failed", "phase_message": fatal_error, "error": fatal_error}

    if sync_requested and not fatal_error:
        try:
            sync_service = SyncService(settings, database, LlmClient(settings))
            sync_performed = True
            sync_result = asyncio.run(sync_service.run())
            status = sync_result.to_dict()
            fatal_error = str(status.get("error") or "")
        except Exception as error:
            fatal_error = f"{type(error).__name__}: {error}"
            status = {"phase": "failed", "phase_message": fatal_error, "error": fatal_error}

    sync = {
        "requested": sync_requested,
        "performed": sync_performed,
        "status": status,
    }
    try:
        result = export_jobs(
            database,
            args.output_dir,
            sync=sync,
            source_names={source.id: source.name for source in settings.load_sources()},
        )
    except Exception as error:
        print(f"Export failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    phase = "skipped" if not sync_requested else str((status or {}).get("phase") or "failed")
    paths = [str(path) for path in result.paths]
    _emit(
        as_json,
        {
            "sync": {**sync, "phase": phase},
            "recommended_jobs": result.recommended_jobs_count,
            "all_jobs": result.all_jobs_count,
            "exports": paths,
        },
        [
            f"Sync: {phase}",
            f"Recommended jobs: {result.recommended_jobs_count}",
            f"All jobs: {result.all_jobs_count}",
            "Exports:",
            *(f"  {path}" for path in paths),
        ],
    )

    source_errors = int((status or {}).get("source_errors") or 0)
    if source_errors:
        print(f"Warning: refresh completed with {source_errors} source error(s).", file=sys.stderr)
    if fatal_error:
        print(f"Refresh failed; existing local jobs were exported: {fatal_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    main()
