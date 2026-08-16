from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import uvicorn

from .company import CompanyResearchService
from .config import Settings
from .database import Database
from .evaluation import run_model_evaluation, run_rules_evaluation
from .llm import LlmClient
from .migration import import_legacy
from .setup import LocalModelService, SetupService
from .sync import SyncService


def main() -> None:
    parser = argparse.ArgumentParser(prog="rolebeacon")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Run the local web application")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    subparsers.add_parser("sync", help="Run one incremental sync")
    subparsers.add_parser("status", help="Show source state and database statistics")
    subparsers.add_parser("doctor", help="Check setup, storage, database, and model readiness")
    setup = subparsers.add_parser("setup", help="Validate and import SetupPayloadV1 JSON")
    setup.add_argument("--from-json", type=Path, required=True)
    setup.add_argument("--activate", action="store_true", help="Explicitly activate collection after import")
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

    settings = Settings.load()
    settings.ensure_directories()
    if args.command == "migrate":
        print(json.dumps(import_legacy(settings, args.legacy_root), indent=2))
        return
    if args.command == "setup":
        payload = json.loads(args.from_json.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("Setup JSON must be an object")
        payload["activate"] = bool(args.activate)
        service = SetupService(settings)
        service.validate_setup_payload(payload)
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
    database = Database(settings.database_path)
    database.initialize()

    if args.command == "serve":
        from .app import create_app

        uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port)
    elif args.command == "sync":
        sync_service = SyncService(settings, database, LlmClient(settings))
        sync_result = asyncio.run(sync_service.run())
        print(json.dumps(sync_result.to_dict(), indent=2))
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


if __name__ == "__main__":
    main()
