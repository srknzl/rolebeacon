from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
import tldextract

from .collectors import USER_AGENT, default_http_client, plain_text
from .config import Settings
from .database import Database, company_key
from .llm import LlmClient, LlmUnavailable

COMPANY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
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
    "required": ["summary", "remote_policy", "sponsorship", "relocation", "engineering_signals", "risks", "confidence", "score"],
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


SPONSORSHIP_ABSENT_PHRASES = (
    "no sponsorship", "without sponsorship", "do not sponsor", "does not offer visa sponsorship",
    "doesn't offer visa sponsorship", "not offer visa sponsorship", "unable to sponsor",
)
SPONSORSHIP_PRESENT_PHRASES = ("visa sponsorship", "blue card", "we sponsor", "sponsor visas")
RELOCATION_PHRASES = ("relocation support", "relocation package", "relocation assistance", "relocation allowance")
# Brand pages and registry stubs describe the company. They never state hiring terms, so they are
# excluded from every fit signal — reading them is what let marketing copy score as engineering.
NON_HIRING_SOURCE_TYPES = {"about", "public_registry"}
# Used only when the profile configures no preferred skills, so the dimension still means something.
ENGINEERING_FALLBACK_TERMS = ("distributed", "backend", "platform", "cloud", "open source", "engineering")
# Sentences without one of these words describe the brand, not the terms of being hired.
HIRING_EXCERPT_TERMS = (
    "remote", "hybrid", "on-site", "onsite", "office", "work from", "relocat", "visa", "sponsor",
    "salary", "compensation", "pay", "benefit", "engineer", "developer", "hiring", "candidate",
    "applicant", "interview", "team", "role", "position",
)
# A quote exists to be read beside the claim. Job descriptions run whole sections together without
# a full stop, so an untrimmed "sentence" can be the entire posting, and a navigation label like
# "Remote work" is a match that proves nothing.
QUOTE_MAX_CHARS = 240
QUOTE_MIN_CHARS = 25
# A page that returns 200 with this wording is a soft 404 and must not count as a fetched source.
SOFT_404_TITLE_PHRASES = ("404", "not found", "page unavailable")
SOFT_404_BODY_PHRASES = ("page not found", "page you requested", "page does not exist", "page doesn't exist")

REMOTE_POLICY_WORDING = {
    "worldwide": "described as worldwide",
    "regional": "described as regional, so it does not by itself establish eligibility from your country",
    "hybrid": "described as hybrid",
    "onsite": "described as on-site",
    "unknown": "not stated in the fetched sources",
}

_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)


@dataclass(slots=True)
class CompanyResearchStatus:
    running: bool = False
    company_name: str = ""
    company_id: int | None = None
    phase: str = "idle"
    message: str = "Ready"
    progress_percent: int = 0
    llm_used: bool = False
    used_rules_fallback: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CompanyResearchService:
    def __init__(self, settings: Settings, database: Database, llm: LlmClient):
        self.settings = settings
        self.database = database
        self.llm = llm

    async def research(self, name: str, progress: Any = None) -> int:
        report = progress or (lambda _phase, _message, _percent: None)
        report("collected_jobs", "Reading current job evidence", 12)
        registry = self._registry_entry(name)
        jobs = self.database.company_jobs(name)
        evidence = self._job_evidence(jobs)
        # A refresh re-reads the same handful of pages. Sending back what was stored lets the
        # server answer 304 instead of shipping the whole page again.
        cache = self.database.company_evidence_cache(name)
        domain = registry.get("domain", "") if registry else ""
        if not domain:
            report("official_sources", "Checking the public company registry", 24)
            domain, registry_evidence = await self._wikidata_entry(name)
            evidence.extend(registry_evidence)
        if registry:
            report("official_sources", "Fetching configured official sources", 32)
            evidence.extend(await self._fetch_official_sources(registry.get("sources", []), cache))
        if domain:
            evidence.extend(
                await self._fetch_official_sources(self._conventional_official_sources(domain), cache)
            )
        if self.settings.company_search_api_key:
            report("official_sources", "Discovering current official company pages", 42)
            discovered_domain, discovered_sources = await self._discover_official_sources(name, domain)
            domain = domain or discovered_domain
            evidence.extend(await self._fetch_official_sources(discovered_sources, cache))
        evidence = self._deduplicate_evidence(evidence)
        if not evidence:
            raise ValueError("No configured official sources or collected jobs are available for this company")

        search_profile = self.settings.load_search_profile()
        provider = "rules"
        model = "company-rules-v2"
        if self.settings.llm_enabled:
            if not await self.llm.available():
                health = await self.llm.health()
                raise LlmUnavailable(
                    "LLM unavailable: "
                    f"{health.get('error') or 'the configured endpoint did not provide the selected model'}. "
                    "Fix the model in Settings or explicitly choose Rules only, then try company research again."
                )
            report("llm_analysis", "Analyzing evidence with the configured model", 60)
            last_error: Exception | None = None
            for _attempt in range(2):
                try:
                    result = await self._llm_research(name, evidence, search_profile)
                    self._validate_company_result(result, evidence)
                    profile = {key: value for key, value in result.items() if key != "score"}
                    score = result["score"]
                    self._validate_score(score)
                    provider = "openai-compatible"
                    model = self.settings.llm_model
                    break
                except (LlmUnavailable, ValueError) as error:
                    last_error = error
                    continue
            else:
                raise LlmUnavailable(
                    "The configured model returned an invalid company assessment after two attempts: "
                    f"{last_error}. Fix the model or explicitly choose Rules only, then try again."
                )
        else:
            profile, score = self._deterministic_research(name, evidence, jobs, search_profile)
        coverage, coverage_score = self._fact_coverage(profile, jobs)
        profile["confidence"] = coverage
        score["dimensions"]["evidence_confidence"] = coverage_score
        score["total"] = sum(int(value) for value in score["dimensions"].values())
        report("saving", "Saving the evidence-backed company profile", 92)
        company_id = self.database.save_company_research(
            name=name,
            domain=domain,
            profile=profile,
            evidence=evidence,
            score=score,
            provider=provider,
            model=model,
        )
        report("complete", "Company research complete", 100)
        return company_id

    def _registry_entry(self, name: str) -> dict[str, Any] | None:
        key = company_key(name)
        return next((item for item in self.settings.load_company_registry() if company_key(item["name"]) == key), None)

    async def _discover_official_sources(self, name: str, domain: str) -> tuple[str, list[dict[str, str]]]:
        queries = [f'"{name}" official company about careers']
        if domain:
            queries = [f"site:{domain} about careers engineering remote sponsorship relocation"]
        candidates: list[dict[str, str]] = []
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for query in queries:
                response = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": 10, "search_lang": "en", "safesearch": "strict"},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.settings.company_search_api_key},
                )
                response.raise_for_status()
                for result in response.json().get("web", {}).get("results", []):
                    url = str(result.get("url", ""))
                    title = str(result.get("title", ""))
                    host = self._registrable_host(urlsplit(url).hostname or "")
                    if not host or self._excluded_discovery_host(host):
                        continue
                    if not domain and company_key(name) not in company_key(title):
                        continue
                    if not domain:
                        domain = host
                    if host != self._registrable_host(domain):
                        continue
                    source_type = self._source_type(url, title)
                    candidates.append({"url": url, "type": source_type})
        unique = {item["url"]: item for item in candidates}
        ordered = sorted(unique.values(), key=lambda item: {"about": 0, "careers": 1, "engineering": 2, "official": 3}[item["type"]])
        return domain, ordered[:5]

    async def _wikidata_entry(self, name: str) -> tuple[str, list[dict[str, str]]]:
        """Use Wikidata only to discover an official domain without an API key."""
        params: dict[str, str | int] = {
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "format": "json",
            "limit": 5,
            "type": "item",
            "origin": "*",
        }
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
                response = await client.get("https://www.wikidata.org/w/api.php", params=params)
                response.raise_for_status()
                candidates = response.json().get("search", [])
                exact = next(
                    (item for item in candidates if company_key(str(item.get("label", ""))) == company_key(name)),
                    None,
                )
                if not exact:
                    return "", []
                entity_id = str(exact["id"])
                entity_response = await client.get(f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json")
                entity_response.raise_for_status()
                entity = entity_response.json().get("entities", {}).get(entity_id, {})
                website = self._claim_string(entity.get("claims", {}), "P856")
                host = self._registrable_host(urlsplit(website).hostname or "")
                if not host or self._excluded_discovery_host(host):
                    return "", []
                evidence = [{
                    "source_url": f"https://www.wikidata.org/wiki/{entity_id}",
                    "source_type": "public_registry",
                    "title": f"Official website registry entry for {name}",
                    "excerpt": f"Official website: {website}",
                }]
                return host, evidence
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return "", []

    @staticmethod
    def _claim_string(claims: dict[str, Any], property_id: str) -> str:
        try:
            return str(claims[property_id][0]["mainsnak"]["datavalue"]["value"])
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _conventional_official_sources(domain: str) -> list[dict[str, str]]:
        # Pages where an employer states hiring terms. About and company pages are deliberately
        # absent: they carry brand copy, and reading them inflated the term-hit scores below
        # without ever establishing remote policy, sponsorship, or relocation.
        origin = f"https://{domain}"
        return [
            {"url": f"{origin}/careers", "type": "careers"},
            {"url": f"{origin}/jobs", "type": "careers"},
            {"url": f"{origin}/careers/benefits", "type": "careers"},
            {"url": f"{origin}/engineering", "type": "engineering"},
            {"url": f"{origin}/blog/engineering", "type": "engineering"},
        ]

    @staticmethod
    def _registrable_host(host: str) -> str:
        normalized = host.casefold().removeprefix("www.")
        extracted = _TLD_EXTRACT(normalized)
        return extracted.top_domain_under_public_suffix or normalized

    @staticmethod
    def _excluded_discovery_host(host: str) -> bool:
        blocked = {
            "linkedin.com", "crunchbase.com", "wikipedia.org", "glassdoor.com", "indeed.com",
            "greenhouse.io", "lever.co", "ashbyhq.com", "himalayas.app", "remoteok.com",
        }
        return host in blocked

    @staticmethod
    def _source_type(url: str, title: str) -> str:
        text = f"{url} {title}".casefold()
        if "career" in text or "/jobs" in text:
            return "careers"
        if "engineering" in text or "developer" in text:
            return "engineering"
        if "about" in text or "company" in text:
            return "about"
        return "official"

    async def _fetch_official_sources(
        self,
        sources: list[dict[str, str]],
        cache: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        stored = cache or {}
        evidence = []
        robots_cache: dict[str, RobotFileParser] = {}
        async with default_http_client() as client:
            for source in sources[:5]:
                url = source["url"]
                if not await self._allowed(client, url, robots_cache):
                    continue
                previous = stored.get(url, {})
                headers = {}
                if previous.get("etag"):
                    headers["If-None-Match"] = str(previous["etag"])
                if previous.get("last_modified"):
                    headers["If-Modified-Since"] = str(previous["last_modified"])
                try:
                    response = await client.get(url, headers=headers)
                    if response.status_code == 304:
                        evidence.append(
                            {
                                "source_url": url,
                                "source_type": source.get("type", str(previous["source_type"])),
                                "title": str(previous["title"]),
                                "excerpt": str(previous["excerpt"]),
                                "etag": str(previous.get("etag") or ""),
                                "last_modified": str(previous.get("last_modified") or ""),
                            }
                        )
                        continue
                    response.raise_for_status()
                    requested_host = self._registrable_host(urlsplit(url).hostname or "")
                    final_host = self._registrable_host(response.url.host or "")
                    if not requested_host or final_host != requested_host:
                        continue
                    content_type = response.headers.get("content-type", "")
                    if "html" not in content_type and "text" not in content_type:
                        continue
                    text = plain_text(response.text)[:16000]
                    if len(text) < 100:
                        continue
                    title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.IGNORECASE | re.DOTALL)
                    title = plain_text(title_match.group(1)) if title_match else "Official company page"
                    if self._soft_404(title, text):
                        continue
                    validated_type = self._validated_source_type(source.get("type", "official"), str(response.url), title, text)
                    evidence.append(
                        {
                            "source_url": str(response.url),
                            "source_type": validated_type,
                            "title": title,
                            "excerpt": text,
                            "etag": response.headers.get("etag", ""),
                            "last_modified": response.headers.get("last-modified", ""),
                        }
                    )
                except httpx.HTTPError:
                    continue
        return evidence

    @classmethod
    def _validated_source_type(cls, requested: str, final_url: str, title: str, text: str) -> str:
        detected = cls._source_type(final_url, title)
        hiring_context = any(term in f"{title} {text[:1200]}".casefold() for term in ("career", "jobs", "join our team", "open roles", "benefits"))
        if requested == "careers" and (detected != "careers" or not hiring_context):
            return "official"
        if requested == "engineering" and detected != "engineering":
            return "official"
        return requested

    @staticmethod
    def _soft_404(title: str, text: str) -> bool:
        """Some sites answer 200 for a missing page. Counting it as a source would fake coverage."""
        return any(phrase in title.casefold() for phrase in SOFT_404_TITLE_PHRASES) or any(
            phrase in text[:600].casefold() for phrase in SOFT_404_BODY_PHRASES
        )

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
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for job in jobs:
            family = " ".join(str(job.get("normalized_title", "")).split()[:2])
            groups.setdefault((str(job.get("location_bucket", "")), family), []).append(job)
        sample: list[dict[str, Any]] = []
        while len(sample) < 20 and any(groups.values()):
            for key in sorted(groups):
                if groups[key] and len(sample) < 20:
                    sample.append(groups[key].pop(0))
        evidence = []
        for job in sample:
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

    @staticmethod
    def _deduplicate_evidence(evidence: list[dict[str, str]]) -> list[dict[str, str]]:
        unique: dict[str, dict[str, str]] = {}
        for item in evidence:
            excerpt_key = re.sub(r"\W+", " ", item.get("excerpt", "").casefold()).strip()
            key = hashlib.sha256(excerpt_key.encode()).hexdigest() if excerpt_key else item.get("source_url", "")
            current = unique.get(key)
            if current is None or current.get("source_type") == "current_job_posting" and item.get("source_type") != "current_job_posting":
                unique[key] = item
        return list(unique.values())

    @staticmethod
    def _fact_coverage(profile: dict[str, Any], jobs: list[dict[str, Any]]) -> tuple[float, int]:
        """Count the hiring facts the sources established, not how many URLs answered 200.

        Fetching five brand pages proves nothing about whether this employer sponsors a visa,
        so the old source count read as confidence while measuring effort.
        """
        known = sum(
            (
                profile.get("remote_policy", "unknown") != "unknown",
                profile.get("sponsorship", "unknown") != "unknown",
                profile.get("relocation", "unknown") != "unknown",
                any(job.get("salary_min") or job.get("salary_max") for job in jobs),
                bool(profile.get("engineering_signals")),
            )
        )
        return round(0.2 + 0.14 * known, 2), 2 * known

    async def _llm_research(
        self,
        name: str,
        evidence: list[dict[str, str]],
        search_profile: dict[str, Any],
    ) -> dict[str, Any]:
        compact = [
            {
                "source_url": item["source_url"],
                "source_type": item["source_type"],
                "content": self._hiring_excerpt(item["excerpt"]),
            }
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
        hiring = [item for item in evidence if item["source_type"] not in NON_HIRING_SOURCE_TYPES]
        text = " ".join(item["excerpt"] for item in hiring).casefold()
        domains = search_profile.get("preferred_domains", [])
        domain_hits = [item for item in domains if item.casefold() in text]
        skills = [str(value) for value in search_profile.get("preferred_skills", [])]
        def term_present(term: str, value: str) -> bool:
            return bool(re.search(rf"(?<![\w]){re.escape(term.casefold())}(?![\w])", value))

        stack_hits = [item for item in (skills or ENGINEERING_FALLBACK_TERMS) if term_present(item, text)]
        remote_policy, remote_source, remote_quote = self._infer_remote_policy(hiring)
        no_sponsor_found = self._locate(hiring, SPONSORSHIP_ABSENT_PHRASES)
        sponsor_found = [
            pair
            for pair in self._locate(hiring, SPONSORSHIP_PRESENT_PHRASES)
            if not any(phrase in pair[1].casefold() for phrase in SPONSORSHIP_ABSENT_PHRASES)
        ]
        relocation_found = self._locate(hiring, RELOCATION_PHRASES)
        # A single posting states one role's terms. Company-wide claims need an official page or agreement.
        official_no_sponsor = self._company_level(no_sponsor_found)
        sponsor = self._company_level(sponsor_found)
        relocation = self._company_level(relocation_found)
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
            "engineering_environment": min(20, 4 + 4 * len(stack_hits)),
            "location_mobility": mobility_score,
            "compensation": 10 if salary_known else 5,
            "company_quality": 8 if target else 5,
            # research() replaces this once the profile exists, because coverage counts
            # established facts and none of them are known yet here.
            "evidence_confidence": 0,
        }
        domain_found = self._locate(hiring, tuple(item.casefold() for item in domain_hits))
        stack_evidence = [
            (skill, self._locate(hiring, (skill.casefold(),))) for skill in stack_hits
        ]
        reasons = []
        # No fallback source: a claim citing a page that does not contain the term is worse than an
        # uncited claim, because the "Source" link then looks like verification.
        if domain_found:
            reasons.append(
                self._claim(f"Evidence mentions preferred domains: {', '.join(domain_hits)}", domain_found[0])
            )
        engineering_signals = [
            self._claim(f"Engineering signal: {skill}", found[0]) for skill, found in stack_evidence if found
        ]
        reasons.extend(engineering_signals)
        if remote_policy == "worldwide" and remote_source:
            reasons.append(
                {
                    "claim": "A source explicitly describes worldwide remote work",
                    "source_url": remote_source["source_url"],
                    "quote": remote_quote,
                }
            )
        if sponsor and sponsor_found:
            reasons.append(self._claim("A source states that visa sponsorship is offered", sponsor_found[0]))
        if relocation and relocation_found:
            reasons.append(self._claim("A source states that relocation support is offered", relocation_found[0]))
        risks = []
        if no_sponsor_found:
            risks.append(
                self._claim(
                    "At least one source states that sponsorship is unavailable for the company or a role",
                    no_sponsor_found[0],
                )
            )
        if remote_policy in {"regional", "hybrid"} and remote_source:
            risks.append(
                {
                    "claim": "Remote availability appears geographically scoped and does not establish eligibility from the candidate's configured country",
                    "source_url": remote_source["source_url"],
                    "quote": remote_quote,
                }
            )
        if not sponsor and not relocation:
            # An absence is established by no single page, so this claim deliberately cites none.
            risks.append(
                {"claim": "Sponsorship and relocation support could not be confirmed", "source_url": "", "quote": ""}
            )
        sponsorship_state = "unavailable" if official_no_sponsor else "available" if sponsor else "unknown"
        relocation_state = "available" if relocation else "unknown"
        profile = {
            "summary": self._factual_summary(
                remote_policy, sponsorship_state, relocation_state, jobs, domain_hits
            ),
            "remote_policy": remote_policy,
            "sponsorship": sponsorship_state,
            "relocation": relocation_state,
            "engineering_signals": engineering_signals,
            "risks": risks,
        }
        score = {"total": sum(dimensions.values()), "dimensions": dimensions, "reasons": reasons, "risks": risks}
        return profile, score

    @staticmethod
    def _factual_summary(
        remote_policy: str,
        sponsorship: str,
        relocation: str,
        jobs: list[dict[str, Any]],
        domain_hits: list[str],
    ) -> str:
        """State what the evidence established. Quoting the employer's own description assessed nothing."""
        def stated(value: str) -> str:
            return "not stated in the fetched sources" if value == "unknown" else f"stated as {value}"

        parts = [
            f"Remote work is {REMOTE_POLICY_WORDING[remote_policy]}.",
            f"Visa sponsorship is {stated(sponsorship)}.",
            f"Relocation support is {stated(relocation)}.",
        ]
        if jobs:
            parts.append(f"{len(jobs)} collected job posting{'' if len(jobs) == 1 else 's'} informed this assessment.")
        if domain_hits:
            parts.append(f"Sources mention your preferred domains: {', '.join(domain_hits)}.")
        return " ".join(parts)

    @staticmethod
    def _sentences(value: str) -> list[str]:
        return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", value) if part.strip()]

    @classmethod
    def _locate(
        cls,
        evidence: list[dict[str, str]],
        patterns: tuple[str, ...],
    ) -> list[tuple[dict[str, str], str]]:
        """Every (source, sentence) pair where a source actually states the fact, so a claim can quote it.

        Matching the whole page told us a word appeared somewhere on it. Matching a sentence tells
        us what the employer said, which is the only thing worth showing next to a Source link.
        """
        found = []
        for item in evidence:
            matches = []
            for sentence in cls._sentences(item["excerpt"]):
                folded = sentence.casefold()
                hits = [
                    match.start()
                    for pattern in patterns
                    if (match := re.search(rf"(?<![\w]){re.escape(pattern)}(?![\w])", folded))
                ]
                if hits:
                    matches.append((item, cls._window(sentence, min(hits))))
            if matches:
                found.append(cls._preferred(matches))  # one quoted sentence per source is enough
        return found

    @staticmethod
    def _preferred(pairs: list[tuple[dict[str, str], str]]) -> tuple[dict[str, str], str]:
        """Prefer a sentence long enough to read over a navigation label, then an official page.

        A two-word heading like "Remote work" is a real match and a useless quote, so readability
        outranks provenance here. Which sources may establish the claim is decided elsewhere.
        """
        return min(
            pairs,
            key=lambda pair: (
                len(pair[1]) < QUOTE_MIN_CHARS,
                pair[0]["source_type"] == "current_job_posting",
            ),
        )

    @staticmethod
    def _window(sentence: str, index: int) -> str:
        """Quote around the wording that matched.

        Job descriptions run whole sections together without a full stop, so trimming from the
        start of the "sentence" reliably cuts off the words the claim is actually about.
        """
        if len(sentence) <= QUOTE_MAX_CHARS:
            return sentence
        start = max(0, index - 60)
        window = sentence[start : start + QUOTE_MAX_CHARS]
        if start:
            window = "…" + window.split(" ", 1)[-1]
        if start + QUOTE_MAX_CHARS < len(sentence):
            window = window.rsplit(" ", 1)[0] + "…"
        return window

    @staticmethod
    def _company_level(found: list[tuple[dict[str, str], str]]) -> bool:
        """An official page states company policy. A job posting states one role's terms.

        So a claim carried only by postings needs two of them agreeing before it is company-wide.
        """
        if any(pair[0]["source_type"] != "current_job_posting" for pair in found):
            return True
        return len({pair[0]["source_url"] for pair in found}) >= 2

    @staticmethod
    def _claim(claim: str, found: tuple[dict[str, str], str]) -> dict[str, str]:
        return {"claim": claim, "source_url": found[0]["source_url"], "quote": found[1]}

    @classmethod
    def _hiring_excerpt(cls, excerpt: str, limit: int = 4000) -> str:
        """Send the model the sentences that state hiring terms rather than a whole marketing page."""
        kept = " ".join(
            sentence
            for sentence in cls._sentences(excerpt)
            if any(term in sentence.casefold() for term in HIRING_EXCERPT_TERMS)
        )
        return (kept or excerpt)[:limit]

    @staticmethod
    def _remote_policy_for_text(value: str) -> tuple[str, int]:
        """The policy this wording states, and where it says so, so the claim can quote that part."""
        text = value.casefold()
        for policy, patterns in (
            ("regional", REMOTE_SCOPED_PATTERNS),
            ("worldwide", REMOTE_WORLDWIDE_PATTERNS),
            ("hybrid", (r"\bhybrid(?:[- ]work|\s+role|\s+position|\s+team)?\b",)),
            ("regional", REMOTE_REGIONAL_PATTERNS),
            ("onsite", (r"\b(?:on[- ]?site|in[- ]office)\b",)),
        ):
            match = next((found for found in (re.search(item, text) for item in patterns) if found), None)
            if match:
                if policy == "regional" and "within" in match.group(0) and not re.search(
                    r"\b(?:remote|work location|country of employment|hiring|based|located|resident)\b", text
                ):
                    continue
                return policy, match.start()
        return "unknown", 0

    @classmethod
    def _infer_remote_policy(
        cls,
        evidence: list[dict[str, str]],
    ) -> tuple[str, dict[str, str] | None, str]:
        found: dict[str, list[tuple[dict[str, str], str]]] = {}
        for item in evidence:
            for sentence in cls._sentences(item["excerpt"]):
                policy, index = cls._remote_policy_for_text(sentence)
                if policy != "unknown":
                    found.setdefault(policy, []).append((item, cls._window(sentence, index)))
        for policy in ("worldwide", "regional", "hybrid", "onsite"):
            pairs = found.get(policy, [])
            if not pairs:
                continue
            # Only the permissive reading needs corroboration. "Worldwide" from a single posting
            # would overstate where the candidate may actually be hired from; a restrictive
            # wording grants nothing, so one source stating it is enough to record the risk.
            if policy == "worldwide" and not cls._company_level(pairs):
                continue
            pair = cls._preferred(pairs)
            return policy, pair[0], pair[1]
        return "unknown", None, ""

    @staticmethod
    def _validate_score(score: dict[str, Any]) -> None:
        if sum(int(value) for value in score["dimensions"].values()) != int(score["total"]):
            raise LlmUnavailable("Company score dimensions do not add up to total")

    @classmethod
    def _validate_company_result(cls, result: dict[str, Any], evidence: list[dict[str, str]]) -> None:
        expected = {"summary", "remote_policy", "sponsorship", "relocation", "engineering_signals", "risks", "confidence", "score"}
        if set(result) != expected:
            raise ValueError("Company assessment returned unexpected or missing keys")
        allowed_urls = {item["source_url"] for item in evidence}
        for collection in (result["engineering_signals"], result["risks"], result["score"]["reasons"], result["score"]["risks"]):
            for claim in collection:
                source_url = str(claim.get("source_url", ""))
                if source_url and source_url not in allowed_urls:
                    raise ValueError("Company assessment cited evidence that was not fetched")
        if not 0 <= float(result["confidence"]) <= 1:
            raise ValueError("Company assessment confidence is outside 0..1")
        cls._validate_score(result["score"])


class CompanyResearchCoordinator:
    """Keeps one observable local company-research operation for the web interface."""

    def __init__(self, service: CompanyResearchService):
        self.service = service
        self.status = CompanyResearchStatus()
        self._lock = asyncio.Lock()

    def start(self, name: str) -> dict[str, Any]:
        if self.status.running:
            return {"accepted": False, "reason": "already_running", "status": self.status.to_dict()}
        self.status = CompanyResearchStatus(
            running=True,
            company_name=name,
            phase="queued",
            message="Preparing company research",
            progress_percent=3,
        )
        asyncio.create_task(self._run(name))
        return {"accepted": True, "status": self.status.to_dict()}

    async def _run(self, name: str) -> None:
        async with self._lock:
            def report(phase: str, message: str, progress_percent: int) -> None:
                self.status.phase = phase
                self.status.message = message
                self.status.progress_percent = progress_percent
                if phase == "llm_analysis":
                    self.status.llm_used = True
                if phase == "rules_fallback":
                    self.status.used_rules_fallback = True

            try:
                company_id = await self.service.research(name, progress=report)
                self.status.company_id = company_id
                self.status.phase = "complete"
                self.status.message = "Company research complete"
                self.status.progress_percent = 100
            except Exception as error:
                self.status.phase = "failed"
                self.status.message = "Company research failed"
                self.status.error = f"{type(error).__name__}: {error}"
                self.status.progress_percent = 100
            finally:
                self.status.running = False
