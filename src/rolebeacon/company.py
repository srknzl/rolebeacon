from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .collectors import USER_AGENT, default_http_client, plain_text
from .config import Settings
from .database import Database, company_key
from .llm import LlmClient, LlmUnavailable

COMPANY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "industry": {"type": "string"},
        "headquarters": {"type": "string"},
        "size": {"type": "string"},
        "remote_policy": {"type": "string", "enum": ["worldwide", "regional", "hybrid", "onsite", "unknown"]},
        "sponsorship": {"type": "string", "enum": ["available", "unavailable", "unknown"]},
        "relocation": {"type": "string", "enum": ["available", "unavailable", "unknown"]},
        "engineering_signals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"claim": {"type": "string"}, "source_url": {"type": "string"}},
                "required": ["claim", "source_url"],
            },
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"claim": {"type": "string"}, "source_url": {"type": "string"}},
                "required": ["claim", "source_url"],
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "score": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "total": {"type": "integer", "minimum": 0, "maximum": 100},
                "dimensions": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "domain_alignment": {"type": "integer", "minimum": 0, "maximum": 25},
                        "engineering_environment": {"type": "integer", "minimum": 0, "maximum": 20},
                        "location_mobility": {"type": "integer", "minimum": 0, "maximum": 20},
                        "compensation": {"type": "integer", "minimum": 0, "maximum": 15},
                        "company_quality": {"type": "integer", "minimum": 0, "maximum": 10},
                        "evidence_confidence": {"type": "integer", "minimum": 0, "maximum": 10},
                    },
                    "required": ["domain_alignment", "engineering_environment", "location_mobility", "compensation", "company_quality", "evidence_confidence"],
                },
                "reasons": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"claim": {"type": "string"}, "source_url": {"type": "string"}},
                        "required": ["claim", "source_url"],
                    },
                },
                "risks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"claim": {"type": "string"}, "source_url": {"type": "string"}},
                        "required": ["claim", "source_url"],
                    },
                },
            },
            "required": ["total", "dimensions", "reasons", "risks"],
        },
    },
    "required": ["summary", "industry", "headquarters", "size", "remote_policy", "sponsorship", "relocation", "engineering_signals", "risks", "confidence", "score"],
}

REMOTE_WORLDWIDE_PATTERNS = (
    r"\bremote(?:ly)?\s+(?:from\s+)?(?:anywhere|worldwide|globally)\b",
    r"\bwork\s+from\s+anywhere\s+in\s+the\s+world\b",
    r"\bwork\s+from\s+any\s+country\b",
    r"\bhire(?:s|d|ing)?(?:\s+people|\s+talent|\s+employees)?\s+(?:from\s+)?anywhere\s+in\s+the\s+world\b",
)
REMOTE_SCOPED_PATTERNS = (
    r"\b(?:your|the)\s+country\s+of\s+employment\b",
    r"\banywhere\s+in(?:side)?[- ]country\b",
    r"\bwithin\s+(?:your|the)\s+(?:country|region)\b",
    r"\bremote\s+(?:within|in|across)\s+(?:emea|europe|the eu|germany|the united states|the us|canada|india)\b",
    r"\bmust\s+be\s+(?:based|located|resident)\s+in\b",
)
REMOTE_REGIONAL_PATTERNS = (
    r"\bremote[- ]first\b",
    r"\bfully\s+remote\b",
    r"\bremote\s+(?:role|work|employee|position|team)s?\b",
    r"\bwork(?:ing)?\s+remotely\b",
    r"\bdistributed\s+(?:role|workforce|employee|team)s?\b",
)


class CompanyResearchService:
    def __init__(self, settings: Settings, database: Database, llm: LlmClient):
        self.settings = settings
        self.database = database
        self.llm = llm

    async def research(self, name: str) -> int:
        registry = self._registry_entry(name)
        jobs = self.database.company_jobs(name)
        evidence = self._job_evidence(jobs)
        domain = registry.get("domain", "") if registry else ""
        if registry:
            evidence.extend(await self._fetch_official_sources(registry.get("sources", [])))
        if not evidence:
            raise ValueError("No configured official sources or collected jobs are available for this company")

        search_profile = self.settings.load_search_profile()
        if await self.llm.available():
            result = await self._llm_research(name, evidence, search_profile)
            profile = {key: value for key, value in result.items() if key != "score"}
            score = result["score"]
            self._validate_score(score)
            provider = "openai-compatible"
            model = self.settings.llm_model
        else:
            profile, score = self._deterministic_research(name, evidence, jobs, search_profile)
            provider = "rules"
            model = "company-rules-v1"
        return self.database.save_company_research(
            name=name,
            domain=domain,
            profile=profile,
            evidence=evidence,
            score=score,
            provider=provider,
            model=model,
        )

    def _registry_entry(self, name: str) -> dict[str, Any] | None:
        key = company_key(name)
        return next((item for item in self.settings.load_company_registry() if company_key(item["name"]) == key), None)

    async def _fetch_official_sources(self, sources: list[dict[str, str]]) -> list[dict[str, str]]:
        evidence = []
        robots_cache: dict[str, RobotFileParser] = {}
        async with default_http_client() as client:
            for source in sources[:5]:
                url = source["url"]
                if not await self._allowed(client, url, robots_cache):
                    continue
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "html" not in content_type and "text" not in content_type:
                        continue
                    text = plain_text(response.text)[:16000]
                    if len(text) < 100:
                        continue
                    title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.IGNORECASE | re.DOTALL)
                    evidence.append(
                        {
                            "source_url": str(response.url),
                            "source_type": source.get("type", "official"),
                            "title": plain_text(title_match.group(1)) if title_match else "Official company page",
                            "excerpt": text,
                        }
                    )
                except httpx.HTTPError:
                    continue
        return evidence

    async def _allowed(
        self,
        client: httpx.AsyncClient,
        url: str,
        cache: dict[str, RobotFileParser],
    ) -> bool:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if origin not in cache:
            parser = RobotFileParser()
            try:
                response = await client.get(f"{origin}/robots.txt")
                parser.parse(response.text.splitlines() if response.is_success else [])
            except httpx.HTTPError:
                parser.parse([])
            cache[origin] = parser
        return cache[origin].can_fetch(USER_AGENT, url)

    @staticmethod
    def _job_evidence(jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
        evidence = []
        for job in jobs[:20]:
            text = " ".join(
                filter(None, (str(job.get("title", "")), str(job.get("location", "")), str(job.get("description", ""))[:3000]))
            )
            evidence.append(
                {
                    "source_url": str(job.get("canonical_url", "")),
                    "source_type": "current_job_posting",
                    "title": str(job.get("title", "Current job posting")),
                    "excerpt": text,
                }
            )
        return evidence

    async def _llm_research(
        self,
        name: str,
        evidence: list[dict[str, str]],
        search_profile: dict[str, Any],
    ) -> dict[str, Any]:
        compact = [
            {"source_url": item["source_url"], "source_type": item["source_type"], "content": item["excerpt"][:10000]}
            for item in evidence
        ]
        prompt = (
            f"Assess {name} as an employer for this candidate preference profile. Distinguish company fit from "
            "job fit. Use only the supplied evidence. Every engineering signal, reason, and risk must cite one "
            "of the exact supplied source URLs. Unknown facts must remain unknown. Do not infer compensation, "
            "remote eligibility, sponsorship, layoffs, stability, or culture from brand reputation. Treat "
            "'work from anywhere in your country of employment' as regional, never worldwide. One role that "
            "does not sponsor does not prove a company-wide no-sponsorship policy. The score "
            "dimensions must add up to total.\n\n"
            f"PREFERENCES:\n{search_profile}\n\nEVIDENCE:\n{compact}"
        )
        return await self.llm.generate_text(
            "You are a conservative company-research analyst. Return source-grounded structured data only.",
            prompt,
            COMPANY_SCHEMA,
            "company_fit",
        )

    def _deterministic_research(
        self,
        name: str,
        evidence: list[dict[str, str]],
        jobs: list[dict[str, Any]],
        search_profile: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        text = " ".join(item["excerpt"] for item in evidence).casefold()
        domains = search_profile.get("preferred_domains", [])
        domain_hits = [item for item in domains if item.casefold() in text]
        engineering_terms = [term for term in ("distributed", "backend", "platform", "cloud", "open source", "engineering") if term in text]
        remote_policy, remote_source = self._infer_remote_policy(evidence)
        no_sponsor_sources = self._matching_evidence(
            evidence,
            "no sponsorship",
            "without sponsorship",
            "do not sponsor",
            "does not offer visa sponsorship",
            "doesn't offer visa sponsorship",
            "not offer visa sponsorship",
            "unable to sponsor",
        )
        sponsor_sources = [
            item
            for item in self._matching_evidence(evidence, "visa sponsorship", "blue card", "we sponsor")
            if item not in no_sponsor_sources
        ]
        relocation_sources = self._matching_evidence(
            evidence,
            "relocation support",
            "relocation package",
            "relocation assistance",
        )
        official_no_sponsor = any(item["source_type"] != "current_job_posting" for item in no_sponsor_sources)
        sponsor = bool(sponsor_sources)
        relocation = bool(relocation_sources)
        salary_known = any(job.get("salary_min") or job.get("salary_max") for job in jobs)
        target = name in search_profile.get("priority_companies", []) or name in search_profile.get("company_watchlist", [])
        mobility_score = {
            "worldwide": 20,
            "regional": 8,
            "hybrid": 6,
            "onsite": 3,
            "unknown": 4,
        }[remote_policy]
        if sponsor or relocation:
            mobility_score = max(mobility_score, 15)
        dimensions = {
            "domain_alignment": min(25, 8 + 5 * len(domain_hits)),
            "engineering_environment": min(20, 4 + 3 * len(engineering_terms)),
            "location_mobility": mobility_score,
            "compensation": 10 if salary_known else 5,
            "company_quality": 8 if target else 5,
            "evidence_confidence": min(10, 2 + len(evidence) * 2),
        }
        def source_for(*terms: str) -> str:
            return next(
                (
                    item["source_url"]
                    for item in evidence
                    if any(term.casefold() in item["excerpt"].casefold() for term in terms)
                ),
                evidence[0]["source_url"],
            )

        reasons = []
        if domain_hits:
            reasons.append(
                {
                    "claim": f"Evidence mentions preferred domains: {', '.join(domain_hits)}",
                    "source_url": source_for(*domain_hits),
                }
            )
        if engineering_terms:
            reasons.append(
                {
                    "claim": f"Engineering signals include: {', '.join(engineering_terms)}",
                    "source_url": source_for(*engineering_terms),
                }
            )
        if remote_policy == "worldwide" and remote_source:
            reasons.append(
                {
                    "claim": "A source explicitly describes worldwide remote work",
                    "source_url": remote_source["source_url"],
                }
            )
        risks = []
        if no_sponsor_sources:
            risks.append(
                {
                    "claim": "At least one source states that sponsorship is unavailable for the company or a role",
                    "source_url": no_sponsor_sources[0]["source_url"],
                }
            )
        if remote_policy in {"regional", "hybrid"} and remote_source:
            risks.append(
                {
                    "claim": "Remote availability appears geographically scoped and does not establish eligibility from the candidate's configured country",
                    "source_url": remote_source["source_url"],
                }
            )
        if not sponsor and not relocation:
            risks.append(
                {
                    "claim": "Sponsorship and relocation support could not be confirmed",
                    "source_url": evidence[0]["source_url"],
                }
            )
        summary_source = next(
            (item for item in evidence if item["source_type"] == "about"),
            next((item for item in evidence if item["source_type"] == "careers"), evidence[0]),
        )
        profile = {
            "summary": summary_source["excerpt"][:500],
            "industry": "",
            "headquarters": "",
            "size": "",
            "remote_policy": remote_policy,
            "sponsorship": "unavailable" if official_no_sponsor else "available" if sponsor else "unknown",
            "relocation": "available" if relocation else "unknown",
            "engineering_signals": reasons,
            "risks": risks,
            "confidence": min(0.85, 0.35 + len(evidence) * 0.1),
        }
        score = {"total": sum(dimensions.values()), "dimensions": dimensions, "reasons": reasons, "risks": risks}
        return profile, score

    @staticmethod
    def _matching_evidence(evidence: list[dict[str, str]], *terms: str) -> list[dict[str, str]]:
        return [
            item
            for item in evidence
            if any(term.casefold() in item["excerpt"].casefold() for term in terms)
        ]

    @staticmethod
    def _remote_policy_for_text(value: str) -> str:
        text = value.casefold()
        if any(re.search(pattern, text) for pattern in REMOTE_SCOPED_PATTERNS):
            return "regional"
        if any(re.search(pattern, text) for pattern in REMOTE_WORLDWIDE_PATTERNS):
            return "worldwide"
        if re.search(r"\bhybrid(?:[- ]work|\s+role|\s+position|\s+team)?\b", text):
            return "hybrid"
        if any(re.search(pattern, text) for pattern in REMOTE_REGIONAL_PATTERNS):
            return "regional"
        if re.search(r"\b(?:on[- ]?site|in[- ]office)\b", text):
            return "onsite"
        return "unknown"

    @classmethod
    def _infer_remote_policy(
        cls,
        evidence: list[dict[str, str]],
    ) -> tuple[str, dict[str, str] | None]:
        classified = [(item, cls._remote_policy_for_text(item["excerpt"])) for item in evidence]
        policy_sources = [pair for pair in classified if pair[0]["source_type"] != "current_job_posting"]
        candidates = [pair for pair in policy_sources if pair[1] != "unknown"]
        if not candidates:
            candidates = [pair for pair in classified if pair[1] != "unknown"]
        for policy in ("worldwide", "regional", "hybrid", "onsite"):
            match = next((item for item, value in candidates if value == policy), None)
            if match:
                return policy, match
        return "unknown", None

    @staticmethod
    def _validate_score(score: dict[str, Any]) -> None:
        if sum(int(value) for value in score["dimensions"].values()) != int(score["total"]):
            raise LlmUnavailable("Company score dimensions do not add up to total")
