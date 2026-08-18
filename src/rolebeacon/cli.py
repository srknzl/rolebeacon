from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
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
from .evaluation import run_model_evaluation, run_rules_evaluation
from .job_export import export_jobs
from .llm import LlmClient
from .migration import import_legacy
from .setup import LocalModelService, SetupService
from .sync import SyncService
from .wizard import SetupWizard


def main() -> None:
    parser = argparse.ArgumentParser(prog="rolebeacon")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Run the local web application")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    subparsers.add_parser("sync", help="Run one incremental sync")
    jobs = subparsers.add_parser("jobs", help="Refresh and export ranked job discovery results")
    jobs.add_argument("--no-sync", action="store_true", help="Export the existing local database without refreshing")
    jobs.add_argument("--start-ollama", action="store_true", help="Start an installed Ollama before refreshing")
    jobs.add_argument("--from-json", type=Path, help="Import a complete SetupPayloadV1 before running")
    jobs.add_argument("--output-dir", type=Path, default=Path.cwd(), help="Parent directory for the timestamped export")
    subparsers.add_parser("status", help="Show source state and database statistics")
    subparsers.add_parser("doctor", help="Check setup, storage, database, and model readiness")
    setup = subparsers.add_parser("setup", help="Run the interactive wizard, or import SetupPayloadV1 JSON")
    setup.add_argument("--from-json", type=Path, help="Import a SetupPayloadV1 document instead of asking questions")
    setup.add_argument("--activate", action="store_true", help="Explicitly activate collection after import")
    setup.add_argument(
        "--no-interactive",
        action="store_true",
        help="Refuse to start the interactive wizard; requires --from-json",
    )
    migrate = subparsers.add_parser("migrate", help="Copy data from a legacy Job Radar installation")
    migrate.add_argument("--from", dest="legacy_root", type=Path, required=True)
    model = subparsers.add_parser("model", help="Manage an optional local model runtime")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_commands.add_parser("doctor", help="Detect Ollama and locally available models")
    model_commands.add_parser("start", help="Start an installed Ollama service")
    pull = model_commands.add_parser("pull", help="Explicitly download an Ollama model")
    pull.add_argument("model", nargs="?", default="qwen3:8b")
    test = model_commands.add_parser("test", help="Test an OpenAI-compatible endpoint")
    test.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    test.add_argument("--model", default="qwen3:8b")
    test.add_argument("--api-key", default="")
    evaluate = subparsers.add_parser("evaluate-model", help="Run the repeatable scoring-quality evaluation")
    evaluate.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    evaluate.add_argument("--model", default="qwen3:14b")
    evaluate.add_argument("--api-key", default="")
    evaluate.add_argument("--provider", choices=("ollama", "custom"), default="ollama")
    evaluate.add_argument("--runs", type=int, default=1)
    evaluate.add_argument("--output", type=Path)
    evaluate_rules = subparsers.add_parser("evaluate-rules", help="Run the deterministic scoring-quality evaluation")
    evaluate_rules.add_argument("--runs", type=int, default=3)
    evaluate_rules.add_argument("--output", type=Path)
    research = subparsers.add_parser("research-company", help="Refresh a provenance-backed company profile")
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
    settings.ensure_directories()
    if args.command == "migrate":
        print(json.dumps(import_legacy(settings, args.legacy_root), indent=2))
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
            print(json.dumps(summary, indent=2))
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
        print(
            json.dumps(
                {
                    "setup_complete": saved.setup_complete,
                    "activated": saved.activated,
                    "enabled_source_ids": [source.id for source in saved.load_sources() if source.enabled],
                    "scoring_mode": saved.llm_mode,
                },
                indent=2,
            )
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
        sync_result = asyncio.run(sync_service.run())
        print(json.dumps(sync_result.to_dict(), indent=2))
    elif args.command == "jobs":
        exit_code = _run_jobs_command(args, settings, database)
        if exit_code:
            raise SystemExit(exit_code)
    elif args.command == "status":
        print(json.dumps({"stats": database.dashboard_stats(), "sources": database.list_sources()}, indent=2))
    elif args.command == "research-company":
        research_service = CompanyResearchService(settings, database, LlmClient(settings))
        company_id = asyncio.run(research_service.research(args.company))
        company = database.get_company(company_id) or {}
        print(
            json.dumps(
                {
                    "id": company.get("id"),
                    "name": company.get("name"),
                    "score": company.get("score"),
                    "confidence": company.get("confidence"),
                    "remote_policy": company.get("remote_policy"),
                    "sponsorship": company.get("sponsorship"),
                    "relocation": company.get("relocation"),
                    "evidence_count": len(company.get("evidence", [])),
                },
                indent=2,
            )
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
        print(json.dumps(checks, indent=2))
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
        print(json.dumps(model_result, indent=2))
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
        rendered_report = json.dumps({"model": args.model, "base_url": args.base_url, **report}, indent=2)
        if args.output:
            args.output.write_text(f"{rendered_report}\n", encoding="utf-8")
        print(rendered_report)
        if not report["passed"]:
            raise SystemExit(1)
    elif args.command == "evaluate-rules":
        rules_report = run_rules_evaluation(runs=max(2, args.runs))
        rendered_rules_report = json.dumps(rules_report, indent=2)
        if args.output:
            args.output.write_text(f"{rendered_rules_report}\n", encoding="utf-8")
        print(rendered_rules_report)
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


def _run_jobs_command(args: argparse.Namespace, settings: Settings, database: Database) -> int:
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
        result = export_jobs(database, args.output_dir, sync=sync)
    except Exception as error:
        print(f"Export failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    phase = "skipped" if not sync_requested else str((status or {}).get("phase") or "failed")
    print(f"Sync: {phase}")
    print(f"Recommended jobs: {result.recommended_jobs_count}")
    print(f"All jobs: {result.all_jobs_count}")
    print("Exports:")
    for path in result.paths:
        print(f"  {path}")

    source_errors = int((status or {}).get("source_errors") or 0)
    if source_errors:
        print(f"Warning: refresh completed with {source_errors} source error(s).", file=sys.stderr)
    if fatal_error:
        print(f"Refresh failed; existing local jobs were exported: {fatal_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    main()
