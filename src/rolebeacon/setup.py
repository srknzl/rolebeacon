from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Settings
from .profile import CV_CONVERSION_PROMPT, CandidateProfileV1, SetupPayloadV1, candidate_schema, generate_strategies
from .services import validate_candidate_profile

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

    def schemas(self) -> dict[str, Any]:
        return {
            "candidate": candidate_schema(),
            "setup": SetupPayloadV1.model_json_schema(),
            "cv_conversion_prompt": CV_CONVERSION_PROMPT,
        }

    def validate_profile(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            candidate = CandidateProfileV1.model_validate(value)
        except ValidationError as error:
            return {"valid": False, "errors": error.errors(include_url=False)}
        issues = validate_candidate_profile(candidate.model_dump(mode="json"))
        return {"valid": not issues, "errors": issues, "profile": candidate.model_dump(mode="json")}

    def complete(self, value: dict[str, Any]) -> Settings:
        payload = SetupPayloadV1.model_validate(value)
        profile = payload.candidate.model_dump(mode="json")
        issues = validate_candidate_profile(profile)
        if issues:
            raise ValueError("; ".join(issues))
        available_sources = {source.id for source in self.settings.load_sources()}
        unknown_sources = sorted(set(payload.enabled_source_ids) - available_sources)
        if unknown_sources:
            raise ValueError(f"Unknown source IDs: {', '.join(unknown_sources)}")
        strategies = generate_strategies(payload.candidate, payload.mobility, payload.preferences)
        updated = self.settings.save_setup(
            candidate=profile,
            mobility=payload.mobility.model_dump(mode="json"),
            preferences=payload.preferences.model_dump(mode="json"),
            strategies=[item.model_dump(mode="json") for item in strategies],
            enabled_source_ids=payload.enabled_source_ids,
            llm=payload.llm.model_dump(mode="json"),
            activate=payload.activate,
        )
        self.settings = updated
        return updated


class LocalModelService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def discover(self) -> dict[str, Any]:
        executable = shutil.which("ollama")
        models: list[str] = []
        reachable = False
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get("http://127.0.0.1:11434/api/tags")
                if response.is_success:
                    reachable = True
                    models = [str(item.get("name", "")) for item in response.json().get("models", [])]
        except httpx.HTTPError:
            pass
        return {
            "ollama_installed": executable is not None,
            "ollama_executable": executable or "",
            "ollama_reachable": reachable,
            "models": models,
            "recommended_model": "qwen3:8b",
            "high_quality_model": "qwen3:14b",
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

    def start_ollama(self) -> dict[str, Any]:
        executable = shutil.which("ollama")
        if not executable:
            raise RuntimeError("Ollama is not installed or is not on PATH")
        self.settings.ensure_directories()
        log_path = self.settings.data_dir / "ollama.log"
        log = log_path.open("ab")
        try:
            process = subprocess.Popen(
                [executable, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
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
