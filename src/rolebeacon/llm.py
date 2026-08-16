from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx

from .config import Settings
from .domain import EligibilityResult, ScoreResult
from .scoring import (
    DIMENSION_MAXIMUMS,
    LOCATION_SCORES,
    SCORING_PROMPT_VERSION,
    _apply_score_weights,
    compute_verdict,
)

# location_authorization is deliberately absent: it is a pure lookup from the deterministic
# eligibility result (LOCATION_SCORES, scoring.py), spliced into dimensions in _normalize_score.
# Letting a model set it would let it override the eligibility gate - see CLAUDE.md.
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
                "salary_employment": {"type": "integer", "minimum": 0, "maximum": DIMENSION_MAXIMUMS["salary_employment"]},
            },
            "required": ["role_domain", "stack", "domain_experience", "seniority", "salary_employment"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"requirement": {"type": "string"}, "profile_evidence": {"type": "string"}},
                "required": ["requirement", "profile_evidence"],
            },
        },
        "gaps": {
            "type": "array",
            "maxItems": 6,
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
- salary_employment (0-10): 8-10 confirmed fit, 5 when unstated, 0-4 conflict or material uncertainty.
Location and work authorization are scored separately from the deterministic eligibility result - do not
score or mention them as a dimension.

A strong evidence-backed match should normally total at least 55 of these 85 points. Do not normalize dimensions to 0 or 1.
Use evidence only for positive matches and quote concrete candidate facts in profile_evidence. For zero-score dimensions,
omit evidence entirely and explain the mismatch only in gaps. Every gap requirement must name the exact missing skill,
technology, qualification, or authorization; never use a generic dimension name such as "stack". Never write "absent",
"missing", "none", or "no experience" as profile evidence. A sentence such as "Candidate knows Java, but the role
requires React" is not positive evidence: omit it and add a `React` gap. Return confidence as a decimal from 0 to 1."""

GENERIC_GAP_LABELS = {
    "role domain", "role_domain", "stack", "domain experience", "domain_experience", "seniority",
    "location authorization", "location_authorization", "salary employment", "salary_employment",
}
NEGATIVE_EVIDENCE = (" no experience", " does not ", " doesn't ", " lacks ", " missing ", " absent ")
_TERM_RE = re.compile(r"[a-z0-9+#.]{3,}")


def _profile_terms(compact_profile: dict[str, Any]) -> set[str]:
    return set(_TERM_RE.findall(json.dumps(compact_profile, ensure_ascii=False).casefold()))


class LlmUnavailable(RuntimeError):
    pass


class LlmResponseRejected(LlmUnavailable):
    """The model answered and the answer failed validation after retries - unlike a transport
    failure, this is not a reason to abort the whole sync. sync.py catches this first and falls
    back to the rules score for just this one job."""


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
        profile_terms = _profile_terms(compact_profile)
        last_error: Exception | None = None
        correction_messages = messages
        for _ in range(2):
            content = ""
            try:
                content = await self._chat_content(correction_messages, SCORE_SCHEMA, "job_match_score", 0.1, 900)
                value = json.loads(content)
                self._normalize_score(value, eligibility, search_profile)
                self._validate_score(value)
                self._validate_score_semantics(value, profile_terms)
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
        if isinstance(last_error, httpx.HTTPError):
            raise LlmUnavailable(str(last_error))
        raise LlmResponseRejected(str(last_error or "Model returned an invalid response"))

    async def generate_text(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        name: str,
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Like score()'s retry loop, generalized: an optional validate() gets one correction
        attempt before the caller sees a failure. validate() raises ValueError to reject."""
        if not await self.available():
            raise LlmUnavailable("The configured model server is unavailable")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._prompt_for_model(prompt)},
        ]
        last_error: Exception | None = None
        correction_messages = messages
        for _ in range(2):
            content = ""
            try:
                content = await self._chat_content(correction_messages, schema, name, 0.2, 3_000)
                value = json.loads(content)
                if validate:
                    validate(value)
                return value
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                last_error = error
                if content:
                    correction_messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": f"The previous JSON is invalid: {error}. Return a corrected JSON object."},
                    ]
        if isinstance(last_error, httpx.HTTPError):
            raise LlmUnavailable(str(last_error))
        raise LlmResponseRejected(str(last_error or "Model returned an invalid response"))

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
        # Qwen3's reasoning shares num_predict with the final answer. On real scoring prompts a
        # 2k response budget can end with done_reason=length and an empty content field after the
        # model spends the whole budget thinking, so reserve enough room for both phases. This
        # does not affect non-reasoning models such as the measured qwen2.5 alternative.
        model_cf = self.settings.llm_model.casefold()
        response_tokens = max(max_tokens, 4096) if "qwen3" in model_cf else max_tokens
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            # No "think" key by default: let Ollama use each model's own default. Measured
            # directly against qwen3:14b - forcing think=False collapsed scores to near-zero with
            # no evidence on jobs rules scored 70+ (structured output stayed valid JSON either
            # way; "content" is unaffected by thinking, Ollama returns reasoning separately).
            # Thinking costs 2-4x latency, which is why sync.py only LLM-scores a
            # rules-shortlisted subset of jobs.
            "format": schema,
            "options": {"temperature": temperature, "num_predict": response_tokens, "num_ctx": 16384},
        }
        # qwen3.6 is the one measured exception to the rule above: left at its own default it
        # reasons long enough to exceed llm_timeout_seconds outright (0/4 rubric calls completed
        # in testing), but with think explicitly forced off it matched qwen2.5:14b-instruct-q6_k's
        # pass rate on the same rubric at ~24s median latency. Scoped to "qwen3.6" specifically,
        # not the broader "qwen3" prefix above - qwen3:14b measurably got worse forced off.
        if "qwen3.6" in model_cf:
            payload["think"] = False
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        return headers

    def _prompt_for_model(self, prompt: str) -> str:
        # Only relevant off the ollama-native path: there, _ollama_payload's "think" field (or
        # its absence) is the real lever. A generic OpenAI-compatible endpoint has no such field,
        # so the text suffix is the only way to ask a qwen3 model to skip its <think> block there.
        if self.settings.llm_mode != "ollama" and "qwen3" in self.settings.llm_model.casefold():
            return f"{prompt}\n\n/no_think"
        return prompt

    @staticmethod
    def _validate_score(value: dict[str, Any]) -> None:
        if set(value) != {"dimensions", "confidence", "evidence", "gaps", "total", "verdict"}:
            raise ValueError("Score returned unexpected or missing keys")
        dimensions = value["dimensions"]
        if set(dimensions) != set(DIMENSION_MAXIMUMS):
            raise ValueError("Score dimensions do not match the canonical dimension set")
        for key, maximum in DIMENSION_MAXIMUMS.items():
            if not 0 <= int(dimensions[key]) <= maximum:
                raise ValueError(f"{key} is outside its allowed range")
        if sum(int(item) for item in dimensions.values()) != int(value["total"]):
            raise ValueError("Dimension scores do not add up to total")
        if not 0 <= int(value["total"]) <= 100:
            raise ValueError("Total is outside the allowed range")
        if not 0 <= float(value["confidence"]) <= 1:
            raise ValueError("Confidence is outside the allowed range")

    @staticmethod
    def _validate_score_semantics(value: dict[str, Any], profile_terms: set[str] | None = None) -> None:
        profile_terms = profile_terms or set()
        zero_dimensions = {
            key.casefold() for key, score in value["dimensions"].items() if int(score) == 0
        }
        violations: list[str] = []
        evidence_requirements: set[str] = set()
        for evidence in value["evidence"]:
            requirement = str(evidence["requirement"]).strip().casefold()
            evidence_requirements.add(requirement)
            profile_evidence = str(evidence["profile_evidence"]).strip().casefold()
            padded = f" {profile_evidence} "
            if requirement in zero_dimensions:
                violations.append(f"evidence was supplied for zero-score dimension {requirement}")
            if requirement in GENERIC_GAP_LABELS:
                violations.append(f"generic dimension name used as evidence requirement {requirement}")
            if any(marker in padded for marker in NEGATIVE_EVIDENCE):
                violations.append(f"negative or mismatch text was supplied as evidence for {requirement}")
            if profile_terms and not (set(_TERM_RE.findall(profile_evidence)) & profile_terms):
                violations.append(f"evidence for {requirement} is not grounded in the candidate profile")

        seen_gaps: set[str] = set()
        for gap in value["gaps"]:
            requirement = str(gap["requirement"]).strip().casefold()
            if requirement in GENERIC_GAP_LABELS:
                violations.append(f"generic gap label {requirement}")
            if requirement in seen_gaps:
                violations.append(f"duplicate gap {requirement}")
            if requirement in evidence_requirements:
                violations.append(f"{requirement} was claimed as both evidence and a gap")
            seen_gaps.add(requirement)
        if violations:
            raise ValueError("; ".join(violations))

    @staticmethod
    def _normalize_score(
        value: dict[str, Any], eligibility: EligibilityResult, preferences: dict[str, Any] | None = None
    ) -> None:
        value["dimensions"]["location_authorization"] = LOCATION_SCORES[eligibility.status]
        value["dimensions"] = _apply_score_weights(value["dimensions"], preferences or {})
        value["total"] = sum(int(item) for item in value["dimensions"].values())
        confidence = float(value["confidence"])
        value["confidence"] = confidence / 100 if 1 < confidence <= 100 else confidence
        value["evidence"] = value["evidence"][:6]
        value["gaps"] = value["gaps"][:6]
        value["verdict"] = compute_verdict(eligibility.status, value["total"], eligibility.threshold)
