from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Settings
from .domain import EligibilityResult, EligibilityStatus, ScoreResult
from .scoring import DIMENSION_MAXIMUMS, SCORING_PROMPT_VERSION

SCORE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "dimensions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "role_domain": {"type": "integer", "minimum": 0, "maximum": DIMENSION_MAXIMUMS["role_domain"]},
                "stack": {"type": "integer", "minimum": 0, "maximum": DIMENSION_MAXIMUMS["stack"]},
                "domain_experience": {"type": "integer", "minimum": 0, "maximum": DIMENSION_MAXIMUMS["domain_experience"]},
                "seniority": {"type": "integer", "minimum": 0, "maximum": DIMENSION_MAXIMUMS["seniority"]},
                "location_authorization": {
                    "type": "integer", "minimum": 0, "maximum": DIMENSION_MAXIMUMS["location_authorization"],
                },
                "salary_employment": {"type": "integer", "minimum": 0, "maximum": DIMENSION_MAXIMUMS["salary_employment"]},
            },
            "required": ["role_domain", "stack", "domain_experience", "seniority", "location_authorization", "salary_employment"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
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
    "required": ["dimensions", "confidence", "evidence", "gaps"],
}

SCORING_RUBRIC = """Use the full integer point ranges below. These are additive points, not 0-to-1 ratings.
- role_domain (0-30): 30 exact target role/domain, 22-27 strong overlap, 10-21 partial, 0-9 unrelated.
- stack (0-20): 18-20 nearly all required technologies, 10-17 meaningful overlap, 1-9 weak overlap, 0 none.
- domain_experience (0-10): 9-10 direct proven experience, 5-8 transferable, 1-4 adjacent, 0 none.
- seniority (0-15): 13-15 proven target seniority, 7-12 close, 1-6 mismatch, 0 disqualifying.
- location_authorization (0-15): 15 explicitly eligible, 8-12 likely compatible, 0-5 unknown or risky.
- salary_employment (0-10): 8-10 confirmed fit, 5 when unstated, 0-4 conflict or material uncertainty.

A strong evidence-backed match should normally total at least 70 points. Do not normalize dimensions to 0 or 1.
Use evidence only for positive matches and quote concrete candidate facts in profile_evidence. For zero-score dimensions,
omit evidence entirely and explain the mismatch only in gaps. Every gap requirement must name the exact missing skill,
technology, qualification, or authorization; never use a generic dimension name such as "stack". Never write "absent",
"missing", "none", or "no experience" as profile evidence. A sentence such as "Candidate knows Java, but the role
requires React" is not positive evidence: omit it and add a `React` gap. Return confidence as a decimal from 0 to 1."""

GENERIC_GAP_LABELS = {
    "role domain", "role_domain", "stack", "domain experience", "domain_experience", "seniority",
    "location authorization", "location_authorization", "salary employment", "salary_employment",
}
NEGATIVE_EVIDENCE = (" but ", " no experience", " does not ", " doesn't ", " lacks ", " missing ", " absent ")


class LlmUnavailable(RuntimeError):
    pass


class LlmClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def available(self) -> bool:
        return bool((await self.health())["available"])

    async def health(self) -> dict[str, Any]:
        if not self.settings.llm_enabled:
            return {
                "mode": "rules",
                "available": False,
                "status": "rules_only",
                "endpoint": "",
                "model": "",
                "models": [],
                "error": "Rules-only mode is selected",
            }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{self.settings.llm_base_url}/models",
                    headers=self._headers(),
                )
                response.raise_for_status()
                models = [str(item.get("id", "")) for item in response.json().get("data", [])]
                model_found = self.settings.llm_model in models
                return {
                    "mode": self.settings.llm_mode,
                    "available": model_found,
                    "status": "available" if model_found else "model_missing",
                    "endpoint": self.settings.llm_base_url,
                    "model": self.settings.llm_model,
                    "models": models,
                    "error": "" if model_found else f"Model {self.settings.llm_model} was not listed by the endpoint",
                }
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            return {
                "mode": self.settings.llm_mode,
                "available": False,
                "status": "unavailable",
                "endpoint": self.settings.llm_base_url,
                "model": self.settings.llm_model,
                "models": [],
                "error": f"{type(error).__name__}: {error}",
            }

    async def score(
        self,
        job: dict[str, Any],
        eligibility: EligibilityResult,
        search_profile: dict[str, Any],
        candidate_profile: dict[str, Any],
    ) -> ScoreResult:
        compact_profile = {
            "summary": candidate_profile.get("summary"),
            "location": candidate_profile.get("location"),
            "experience": candidate_profile.get("experience"),
            "projects": candidate_profile.get("projects"),
            "skills": candidate_profile.get("skills"),
            "education": candidate_profile.get("education"),
            "preferences": search_profile,
        }
        prompt = (
            "Evaluate this software-engineering job for the candidate. Use only evidence present in the "
            "candidate profile and job. Do not infer missing skills, work authorization, sponsorship, salary, "
            "or experience. Respect the deterministic eligibility result. Score only the dimensions; RoleBeacon "
            "computes the total deterministically. Every positive claim needs profile evidence and every material "
            f"missing requirement must appear in gaps.\n\nSCORING RUBRIC:\n{SCORING_RUBRIC}\n\n"
            f"CANDIDATE:\n{json.dumps(compact_profile, ensure_ascii=False)}\n\n"
            f"ELIGIBILITY:\n{json.dumps({'status': eligibility.status.value, 'route': eligibility.route, 'reasons': eligibility.reasons, 'risks': eligibility.risks})}\n\n"
            f"JOB:\n{json.dumps({key: job.get(key) for key in ('title', 'company', 'location', 'remote_scope', 'employment_type', 'salary_min', 'salary_max', 'salary_currency')}, ensure_ascii=False)}\n"
            f"DESCRIPTION:\n{str(job.get('description', ''))[:20000]}"
        )
        messages = [
            {"role": "system", "content": "You are a conservative job-fit evaluator. Return valid JSON only."},
            {"role": "user", "content": self._prompt_for_model(prompt)},
        ]
        last_error: Exception | None = None
        correction_messages = messages
        for _ in range(2):
            content = ""
            try:
                content = await self._chat_content(correction_messages, SCORE_SCHEMA, "job_match_score", 0.1, 900)
                value = json.loads(content)
                self._normalize_score(value, eligibility.status)
                self._validate_score(value)
                self._validate_score_semantics(value)
                return ScoreResult(
                    total=value["total"],
                    dimensions=value["dimensions"],
                    confidence=value["confidence"],
                    verdict=value["verdict"],
                    evidence=value["evidence"],
                    gaps=value["gaps"],
                    provider="ollama" if self.settings.llm_mode == "ollama" else "openai-compatible",
                    model=self.settings.llm_model,
                    prompt_version=f"{SCORING_PROMPT_VERSION}:{self.settings.llm_model}",
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                last_error = error
                if content:
                    correction_messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                f"The previous JSON is invalid: {error}. Return a corrected JSON object. "
                                "Positive evidence must describe only actual matches. Zero-score dimensions have no "
                                "evidence. Replace generic gaps with exact missing technologies, qualifications, or "
                                "authorization constraints from the job. Remove duplicate gaps."
                            ),
                        },
                    ]
        raise LlmUnavailable(str(last_error or "Model returned an invalid response"))

    async def generate_text(self, system: str, prompt: str, schema: dict[str, Any], name: str) -> dict[str, Any]:
        if not await self.available():
            raise LlmUnavailable("The configured model server is unavailable")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._prompt_for_model(prompt)},
        ]
        return json.loads(await self._chat_content(messages, schema, name, 0.2, 3_000))

    async def _chat_content(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        name: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            if self.settings.llm_mode == "ollama":
                base_url = self.settings.llm_base_url.removesuffix("/v1")
                response = await client.post(
                    f"{base_url}/api/chat",
                    headers=self._headers(),
                    json=self._ollama_payload(messages, schema, temperature, max_tokens),
                )
                response.raise_for_status()
                return str(response.json()["message"]["content"])
            payload = {
                "model": self.settings.llm_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": schema},
                },
            }
            response = await client.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])

    def _ollama_payload(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        return {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": schema,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        return headers

    def _prompt_for_model(self, prompt: str) -> str:
        if "qwen3" in self.settings.llm_model.casefold():
            return f"{prompt}\n\n/no_think"
        return prompt

    @staticmethod
    def _validate_score(value: dict[str, Any]) -> None:
        dimensions = value["dimensions"]
        if sum(int(item) for item in dimensions.values()) != int(value["total"]):
            raise ValueError("Dimension scores do not add up to total")
        if not 0 <= int(value["total"]) <= 100:
            raise ValueError("Total is outside the allowed range")
        if not 0 <= float(value["confidence"]) <= 1:
            raise ValueError("Confidence is outside the allowed range")

    @staticmethod
    def _validate_score_semantics(value: dict[str, Any]) -> None:
        zero_dimensions = {
            key.casefold() for key, score in value["dimensions"].items() if int(score) == 0
        }
        violations: list[str] = []
        for evidence in value["evidence"]:
            requirement = str(evidence["requirement"]).strip().casefold()
            profile_evidence = f" {str(evidence['profile_evidence']).strip().casefold()} "
            if requirement in zero_dimensions:
                violations.append(f"evidence was supplied for zero-score dimension {requirement}")
            if any(marker in profile_evidence for marker in NEGATIVE_EVIDENCE):
                violations.append(f"negative or mismatch text was supplied as evidence for {requirement}")

        seen_gaps: set[str] = set()
        for gap in value["gaps"]:
            requirement = str(gap["requirement"]).strip().casefold()
            if requirement in GENERIC_GAP_LABELS:
                violations.append(f"generic gap label {requirement}")
            if requirement in seen_gaps:
                violations.append(f"duplicate gap {requirement}")
            seen_gaps.add(requirement)
        if violations:
            raise ValueError("; ".join(violations))

    @staticmethod
    def _normalize_score(value: dict[str, Any], eligibility_status: EligibilityStatus) -> None:
        value["total"] = sum(int(item) for item in value["dimensions"].values())
        confidence = float(value["confidence"])
        value["confidence"] = confidence / 100 if 1 < confidence <= 100 else confidence
        value["verdict"] = (
            "review"
            if eligibility_status == EligibilityStatus.ELIGIBLE and value["total"] >= 65
            else "reject"
            if value["total"] < 40
            else "low_priority"
        )
