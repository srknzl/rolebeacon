from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Settings
from .domain import EligibilityResult, ScoreResult
from .scoring import SCORING_PROMPT_VERSION

SCORE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "total": {"type": "integer", "minimum": 0, "maximum": 100},
        "dimensions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "role_domain": {"type": "integer", "minimum": 0, "maximum": 25},
                "stack": {"type": "integer", "minimum": 0, "maximum": 20},
                "domain_experience": {"type": "integer", "minimum": 0, "maximum": 20},
                "seniority": {"type": "integer", "minimum": 0, "maximum": 10},
                "location_authorization": {"type": "integer", "minimum": 0, "maximum": 15},
                "salary_employment": {"type": "integer", "minimum": 0, "maximum": 10},
            },
            "required": ["role_domain", "stack", "domain_experience", "seniority", "location_authorization", "salary_employment"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "verdict": {"type": "string", "enum": ["review", "low_priority", "reject"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"requirement": {"type": "string"}, "profile_evidence": {"type": "string"}},
                "required": ["requirement", "profile_evidence"],
            },
        },
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"requirement": {"type": "string"}, "severity": {"type": "string", "enum": ["low", "medium", "high"]}},
                "required": ["requirement", "severity"],
            },
        },
    },
    "required": ["total", "dimensions", "confidence", "verdict", "evidence", "gaps"],
}


class LlmUnavailable(RuntimeError):
    pass


class LlmClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def available(self) -> bool:
        if not self.settings.llm_enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{self.settings.llm_base_url}/models",
                    headers=self._headers(),
                )
                return response.is_success
        except httpx.HTTPError:
            return False

    async def score(
        self,
        job: dict[str, Any],
        eligibility: EligibilityResult,
        search_profile: dict[str, Any],
        candidate_profile: dict[str, Any],
    ) -> ScoreResult:
        compact_profile = {
            "summary": candidate_profile.get("summary"),
            "experience": candidate_profile.get("experience"),
            "projects": candidate_profile.get("projects"),
            "skills": candidate_profile.get("skills"),
            "education": candidate_profile.get("education"),
            "preferences": search_profile,
        }
        prompt = (
            "Evaluate this software-engineering job for the candidate. Use only evidence present in the "
            "candidate profile and job. Do not infer missing skills, work authorization, sponsorship, salary, "
            "or experience. Respect the deterministic eligibility result. Scores must add up to total. Every "
            "positive claim needs profile evidence and every material missing requirement must appear in gaps.\n\n"
            f"CANDIDATE:\n{json.dumps(compact_profile, ensure_ascii=False)}\n\n"
            f"ELIGIBILITY:\n{json.dumps({'status': eligibility.status.value, 'route': eligibility.route, 'reasons': eligibility.reasons, 'risks': eligibility.risks})}\n\n"
            f"JOB:\n{json.dumps({key: job.get(key) for key in ('title', 'company', 'location', 'remote_scope', 'employment_type', 'salary_min', 'salary_max', 'salary_currency')}, ensure_ascii=False)}\n"
            f"DESCRIPTION:\n{str(job.get('description', ''))[:20000]}"
        )
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": "You are a conservative job-fit evaluator. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "job_match_score", "strict": True, "schema": SCORE_SCHEMA},
            },
        }
        last_error: Exception | None = None
        for _ in range(2):
            try:
                async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                    response = await client.post(
                        f"{self.settings.llm_base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"]
                    value = json.loads(content)
                    self._validate_score(value)
                    return ScoreResult(
                        total=value["total"],
                        dimensions=value["dimensions"],
                        confidence=value["confidence"],
                        verdict=value["verdict"],
                        evidence=value["evidence"],
                        gaps=value["gaps"],
                        provider="openai-compatible",
                        model=self.settings.llm_model,
                        prompt_version=f"{SCORING_PROMPT_VERSION}:{self.settings.llm_model}",
                    )
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                last_error = error
        raise LlmUnavailable(str(last_error or "Model returned an invalid response"))

    async def generate_text(self, system: str, prompt: str, schema: dict[str, Any], name: str) -> dict[str, Any]:
        if not await self.available():
            raise LlmUnavailable("The configured model server is unavailable")
        payload = {
            "model": self.settings.llm_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": 0.2,
            "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        }
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return json.loads(response.json()["choices"][0]["message"]["content"])

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _validate_score(value: dict[str, Any]) -> None:
        dimensions = value["dimensions"]
        if sum(int(item) for item in dimensions.values()) != int(value["total"]):
            raise ValueError("Dimension scores do not add up to total")
        if not 0 <= int(value["total"]) <= 100:
            raise ValueError("Total is outside the allowed range")
