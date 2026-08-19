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
    RELOCATION_REGION_CODES,
    SETUP_PLANNING_PROMPT,
    CandidateProfileV1,
    LlmSetup,
    SetupPayloadV1,
    candidate_schema,
    country_names_by_code,
    generate_strategies,
    relocation_countries,
)
from .services import validate_candidate_profile
from .source_discovery import linkedin_source_candidates, relocation_source_candidates

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
READY = "ready"
MISSING = "missing"
AMBIGUOUS = "ambiguous"
# Setup asks for "Google Careers" once and gets one generated source per country, so the payload
# names the family and complete() expands it into the rows it just saved.
SOURCE_SENTINELS = {"__google_careers__": "google_careers", "__amazon_jobs__": "amazon_jobs"}
CLEARANCE_LABELS = {
    "unknown": "Unknown — clearance-restricted roles stay unknown and are never inferred",
    "cannot_meet": "Cannot meet clearance requirements",
    "eligible_to_attempt": "May be eligible to undergo vetting",
    "has_active_clearance": "Holds an active clearance",
}


def _section(value: dict[str, Any], key: str) -> dict[str, Any]:
    section = value.get(key)
    return section if isinstance(section, dict) else {}


def _section_list(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    entries = value.get(key)
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _entries(value: dict[str, Any], key: str) -> list[str]:
    entries = value.get(key)
    if not isinstance(entries, list):
        return []
    return [str(entry).strip() for entry in entries if str(entry).strip()]


def _place(code: str) -> str:
    """Name a country or supported relocation region, falling back to the raw code."""
    if code in RELOCATION_REGION_CODES:
        return RELOCATION_REGION_CODES[code]
    name = country_names_by_code().get(code)
    return f"{name} ({code})" if name else code


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

    def review(self, value: dict[str, Any]) -> dict[str, Any]:
        """Summarize a setup draft and name its missing or ambiguous critical facts.

        Both wizards render exactly these rows, so setup completeness has a single definition.
        A draft is still being edited while this runs, so nothing here validates the schema or
        raises: an absent value is reported as missing rather than rejected.
        """
        candidate = _section(value, "candidate")
        mobility = _section(value, "mobility")
        preferences = _section(value, "preferences")
        llm = _section(value, "llm")
        items: list[dict[str, str]] = []

        def add(title: str, detail: str, status: str = READY) -> None:
            items.append({"title": title, "detail": detail, "status": status})

        name = str(candidate.get("name", "")).strip()
        country_code = str(_section(candidate, "location").get("country_code", "")).strip().upper()
        if name and country_code:
            add("Candidate", f"{name} — currently in {_place(country_code)}")
        elif name:
            add("Candidate", f"{name} — no current country", MISSING)
        else:
            add("Candidate", "No name", MISSING)

        roles = _entries(preferences, "target_roles")
        add("Target roles", ", ".join(roles) or "No target role", READY if roles else MISSING)

        authorizations = [value.upper() for value in _entries(mobility, "work_authorizations")]
        add(
            "Work authorization",
            ", ".join(_place(code) for code in authorizations) or "No country you can work in today",
            READY if authorizations else MISSING,
        )

        targets = [
            str(target.get("country_code", "")).strip().upper()
            for target in _section_list(mobility, "relocation_targets")
        ]
        willing = bool(mobility.get("willing_to_relocate", True))
        if not targets:
            add("Relocation", "No relocation target — searching your own country and remote roles only")
        elif willing:
            add("Relocation", ", ".join(_place(code) for code in targets))
        else:
            add(
                "Relocation",
                f"{len(targets)} target(s) listed while 'willing to relocate' is off, so none of them is searched",
                AMBIGUOUS,
            )

        add(
            "Sponsorship",
            "Required outside your authorized countries"
            if mobility.get("sponsorship_required_outside_authorized_countries", True)
            else "Not required outside your authorized countries",
        )

        clearance = str(_section(mobility, "clearance_policy").get("status", "") or "unknown")
        add(
            "Security clearance",
            CLEARANCE_LABELS.get(clearance, clearance),
            AMBIGUOUS if clearance == "unknown" else READY,
        )

        salary = _section(preferences, "salary")
        minimum = salary.get("minimum")
        currency = str(salary.get("currency", "")).strip().upper()
        hard_filter = bool(salary.get("hard_filter"))
        if minimum in (None, ""):
            add(
                "Salary",
                "Hard filter enabled without a minimum, so it rejects nothing" if hard_filter else "No minimum",
                AMBIGUOUS if hard_filter else READY,
            )
        elif not currency:
            add("Salary", f"Minimum {minimum} with no currency, so no posting is comparable", AMBIGUOUS)
        else:
            add(
                "Salary",
                f"{'Rejects below' if hard_filter else 'Prefers at least'} {minimum} {currency}"
                "; missing or different-currency pay stays unknown",
            )

        sources = _entries(value, "enabled_source_ids")
        add(
            "Sources",
            f"{len(sources)} selected" if sources else "No source selected",
            READY if sources else MISSING,
        )

        mode = str(llm.get("mode", "") or "rules")
        model = str(llm.get("model", "")).strip()
        if mode == "rules":
            add("Scoring", "Rules only — no model is contacted")
        elif model:
            add("Scoring", f"{mode}: {model}")
        else:
            add("Scoring", f"{mode} selected without a model identifier", MISSING)

        return {
            "items": items,
            "missing": [f"{item['title']}: {item['detail']}" for item in items if item["status"] == MISSING],
            "ambiguous": [f"{item['title']}: {item['detail']}" for item in items if item["status"] == AMBIGUOUS],
            "ready": not any(item["status"] == MISSING for item in items),
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
        board_candidates = relocation_source_candidates(countries)
        # LinkedIn takes the raw, unexpanded targets: it resolves a continent as one geography,
        # so expanding EUROPE into 42 country rows would walk the same postings 42 times.
        linkedin_candidates = linkedin_source_candidates(
            [{"name": country_names_by_code().get(code, code)} for code in payload.mobility.work_authorizations]
            + [{"name": item.country_name} for item in payload.mobility.relocation_targets]
        )
        # Everything the payload names is checked before anything is written. The CLI wizard
        # documents that a run it does not finish leaves the configuration untouched, and it
        # saves through this method, so a rejected payload must not leave generated rows behind.
        known_ids = {source.id for source in self.settings.load_sources()}
        known_ids.update(source.id for source in board_candidates + linkedin_candidates)
        unknown_sources = sorted(set(payload.enabled_source_ids) - known_ids - set(SOURCE_SENTINELS))
        if unknown_sources:
            raise ValueError(f"Unknown source IDs: {', '.join(unknown_sources)}")

        generated, _ = self.settings.save_sources(board_candidates)
        self.settings.save_sources(linkedin_candidates)
        enabled_source_ids = list(payload.enabled_source_ids)
        for sentinel, kind in SOURCE_SENTINELS.items():
            if sentinel in enabled_source_ids:
                enabled_source_ids.remove(sentinel)
                enabled_source_ids += [source.id for source in generated if source.kind == kind]
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
