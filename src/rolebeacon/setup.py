from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Settings
from .llm import LlmClient, LlmUnavailable
from .profile import (
    CV_CONVERSION_PROMPT,
    SETUP_PLANNING_PROMPT,
    CandidateProfileV1,
    LlmSetup,
    SetupPayloadV1,
    candidate_schema,
    generate_strategies,
    relocation_countries,
)
from .services import validate_candidate_profile
from .source_discovery import relocation_source_candidates

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class SetupService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def status(self) -> dict[str, Any]:
        return {
            "completed": self.settings.setup_complete,
            "activated": self.settings.activated,
            "candidate_profile_present": self.settings.candidate_profile_path.exists(),
            "llm_mode": self.settings.llm_mode,
            "llm_model": self.settings.llm_model,
            "llm_base_url": self.settings.llm_base_url,
            "rules_only_available": True,
        }

    def saved_payload(self) -> dict[str, Any]:
        """Return the editable setup state without ever returning a stored secret."""
        return {
            "candidate": self.settings.load_candidate_profile(),
            "mobility": self.settings.load_mobility_profile(),
            "preferences": self.settings.load_search_profile(),
            "enabled_source_ids": [source.id for source in self.settings.load_sources() if source.enabled],
            "llm": {
                "mode": self.settings.llm_mode,
                "base_url": self.settings.llm_base_url,
                "model": self.settings.llm_model,
                "api_key": "",
                "api_key_action": "preserve",
                "api_key_configured": bool(self.settings.llm_api_key),
            },
            "activate": self.settings.activated,
        }

    def schemas(self) -> dict[str, Any]:
        return {
            "candidate": candidate_schema(),
            "setup": SetupPayloadV1.model_json_schema(),
            "cv_conversion_prompt": CV_CONVERSION_PROMPT,
            "setup_planning_prompt": SETUP_PLANNING_PROMPT,
        }

    def validate_profile(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            candidate = CandidateProfileV1.model_validate(value)
        except ValidationError as error:
            return {"valid": False, "errors": error.errors(include_url=False, include_context=False)}
        issues = validate_candidate_profile(candidate.model_dump(mode="json"))
        return {"valid": not issues, "errors": issues, "profile": candidate.model_dump(mode="json")}

    def validate_setup_payload(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = SetupPayloadV1.model_validate(value)
        except ValidationError as error:
            return {"valid": False, "errors": error.errors(include_url=False, include_context=False)}
        issues = validate_candidate_profile(payload.candidate.model_dump(mode="json"))
        return {
            "valid": not issues,
            "errors": issues,
            "payload": payload.model_dump(mode="json", exclude={"llm": {"api_key"}}),
        }

    async def plan_with_llm(self, value: dict[str, Any]) -> dict[str, Any]:
        candidate = CandidateProfileV1.model_validate(value.get("candidate", {}))
        llm = LlmSetup.model_validate(value.get("llm", {}))
        if llm.mode == "rules":
            raise ValueError("Choose Ollama or a custom endpoint before asking a model to plan preferences")
        temporary_settings = replace(
            self.settings,
            llm_mode=llm.mode,
            llm_enabled=True,
            llm_base_url=llm.base_url.rstrip("/"),
            llm_model=llm.model,
            llm_api_key=llm.api_key,
        )
        notes = str(value.get("notes", "")).strip() or "No additional notes."
        try:
            result = await LlmClient(temporary_settings).generate_text(
                system="You configure a local-first job discovery tool. Return only JSON matching the supplied schema.",
                prompt=(
                    f"{SETUP_PLANNING_PROMPT}\n\n"
                    f"CANDIDATE PROFILE:\n{json.dumps(candidate.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                    f"CANDIDATE NOTES:\n{notes}"
                ),
                schema=SetupPayloadV1.model_json_schema(),
                name="rolebeacon_setup_plan",
            )
        except (httpx.HTTPError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise LlmUnavailable(str(error)) from error
        result["candidate"] = candidate.model_dump(mode="json")
        result["llm"] = llm.model_dump(mode="json")
        result["activate"] = False
        validated = SetupPayloadV1.model_validate(result)
        return validated.model_dump(mode="json")

    def complete(self, value: dict[str, Any]) -> Settings:
        current = self.saved_payload() if self.settings.setup_complete else {}

        def merged(existing: Any, incoming: Any) -> Any:
            if isinstance(existing, dict) and isinstance(incoming, dict):
                return {key: merged(existing.get(key), item) for key, item in incoming.items()} | {
                    key: item for key, item in existing.items() if key not in incoming
                }
            return incoming

        candidate_value = merged(current.get("candidate", {}), value.get("candidate", {}))
        mobility_value = merged(current.get("mobility", {}), value.get("mobility", {}))
        preferences_value = merged(current.get("preferences", {}), value.get("preferences", {}))
        llm_value = merged(current.get("llm", {}), value.get("llm", {}))
        llm_value.pop("api_key_configured", None)
        complete_value = {
            **current,
            **value,
            "candidate": candidate_value,
            "mobility": mobility_value,
            "preferences": preferences_value,
            "llm": llm_value,
        }
        payload = SetupPayloadV1.model_validate(complete_value)
        profile = payload.candidate.model_dump(mode="json")
        issues = validate_candidate_profile(profile)
        if issues:
            raise ValueError("; ".join(issues))

        # Google/Amazon sources are generated per country, so search coverage must include every
        # country the candidate can already work in (work_authorizations, which always contains
        # current_country_code), not just relocation_targets - otherwise a job posted in the
        # candidate's own country, needing no relocation at all, is never searched for.
        countries = relocation_countries(
            [{"country_code": code} for code in payload.mobility.work_authorizations]
            + [item.model_dump(mode="json") for item in payload.mobility.relocation_targets]
        )
        generated, _ = self.settings.save_sources(relocation_source_candidates(countries))
        enabled_source_ids = list(payload.enabled_source_ids)
        for sentinel, kind in (("__google_careers__", "google_careers"), ("__amazon_jobs__", "amazon_jobs")):
            if sentinel in enabled_source_ids:
                enabled_source_ids.remove(sentinel)
                enabled_source_ids += [source.id for source in generated if source.kind == kind]

        available_sources = {source.id for source in self.settings.load_sources()}
        unknown_sources = sorted(set(enabled_source_ids) - available_sources)
        if unknown_sources:
            raise ValueError(f"Unknown source IDs: {', '.join(unknown_sources)}")
        strategies = generate_strategies(payload.candidate, payload.mobility, payload.preferences)
        updated = self.settings.save_setup(
            candidate=profile,
            mobility=payload.mobility.model_dump(mode="json"),
            preferences=payload.preferences.model_dump(mode="json"),
            strategies=[item.model_dump(mode="json") for item in strategies],
            enabled_source_ids=enabled_source_ids,
            llm=payload.llm.model_dump(mode="json"),
            activate=payload.activate,
        )
        self.settings = updated
        return updated


class LocalModelService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def discover(self, base_url: str = "http://127.0.0.1:11434/v1") -> dict[str, Any]:
        executable = shutil.which("ollama")
        models: list[str] = []
        reachable = False
        ollama_base_url = base_url.rstrip("/")
        if ollama_base_url.endswith("/v1"):
            ollama_base_url = ollama_base_url[:-3]
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{ollama_base_url}/api/tags")
                if response.is_success:
                    reachable = True
                    models = [str(item.get("name", "")) for item in response.json().get("models", [])]
        except httpx.HTTPError:
            pass
        return {
            "ollama_installed": executable is not None,
            "ollama_executable": executable or "",
            "ollama_reachable": reachable,
            "endpoint": base_url,
            "models": models,
            "default_model": "qwen3:8b",
            "high_quality_model": "qwen2.5:14b-instruct-q6_k",
            "rules_only_available": True,
        }

    async def test_endpoint(self, *, base_url: str, model: str, api_key: str = "") -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
                response.raise_for_status()
                available = [str(item.get("id", "")) for item in response.json().get("data", [])]
                return {"ok": True, "model_found": model in available, "models": available}
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def start_ollama(self, *, host: str = "") -> dict[str, Any]:
        executable = shutil.which("ollama")
        if not executable:
            raise RuntimeError("Ollama is not installed or is not on PATH")
        self.settings.ensure_directories()
        log_path = self.settings.data_dir / "ollama.log"
        log = log_path.open("ab")
        try:
            environment = None
            if host:
                environment = {**os.environ, "OLLAMA_HOST": host}
            process = subprocess.Popen(
                [executable, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=environment,
            )
        finally:
            log.close()
        return {"started": True, "pid": process.pid, "log_path": str(log_path)}

    async def pull_ollama_model(self, model: str) -> dict[str, Any]:
        executable = shutil.which("ollama")
        if not executable:
            raise RuntimeError("Ollama is not installed or is not on PATH")
        if not MODEL_NAME.fullmatch(model):
            raise ValueError("Invalid model name")
        process = await asyncio.create_subprocess_exec(
            executable,
            "pull",
            model,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip() or "Ollama model pull failed"
            raise RuntimeError(message)
        return {"pulled": True, "model": model, "output": stdout.decode(errors="replace").strip()[-2000:]}


def read_setup_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
