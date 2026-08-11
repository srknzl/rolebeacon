from __future__ import annotations

import re
from typing import Any

from .domain import EligibilityResult, EligibilityStatus, ScoreResult

SCORING_PROMPT_VERSION = "job-fit-v3"

NO_SPONSOR_PATTERNS = (
    r"no (?:visa )?sponsorship",
    r"(?:unable|not able) to (?:provide|offer) (?:visa )?sponsorship",
    r"must (?:already )?be (?:legally )?authorized to work",
    r"without (?:current or future )?sponsorship",
    r"we do not sponsor",
    r"(?:citizens|permanent residents) only",
)
SPONSOR_PATTERNS = (
    r"visa sponsorship (?:is )?(?:available|provided|offered)",
    r"we (?:can|will) sponsor",
    r"sponsorship available",
    r"blue card",
)
RELOCATION_PATTERNS = (
    r"relocation (?:assistance|package|support)",
    r"relocation (?:is )?(?:available|provided|offered)",
    r"support (?:your )?relocation",
)
WORLDWIDE_PATTERNS = (
    r"work from anywhere in the world",
    r"work from any country",
    r"remote[- ]worldwide",
    r"worldwide remote",
    r"anywhere in the world",
    r"hire(?:s|d|ing)?(?: people| talent| employees)? (?:from )?anywhere in the world",
)
SCOPED_REMOTE_PATTERNS = (
    r"(?:your|the) country of employment",
    r"anywhere in(?:side)?[- ]country",
    r"within (?:your|the) (?:country|region)",
    r"remote (?:within|in|across) [a-z][a-z .-]+",
    r"must be (?:based|located|resident) in",
)
EUROPE_LOCATION_TERMS = (
    "europe", "european union", "eu", "eea", "albania", "andorra", "austria", "belgium", "bosnia",
    "bulgaria", "croatia", "cyprus", "czechia", "czech republic", "denmark", "estonia", "finland",
    "france", "germany", "greece", "hungary", "iceland", "ireland", "italy", "kosovo", "latvia",
    "liechtenstein", "lithuania", "luxembourg", "malta", "moldova", "monaco", "montenegro", "netherlands",
    "north macedonia", "norway", "poland", "portugal", "romania", "san marino", "serbia", "slovakia",
    "slovenia", "spain", "sweden", "switzerland", "ukraine", "united kingdom", "england", "scotland", "wales",
)


def _contains(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _company_in(company: str, values: list[str]) -> bool:
    normalized = re.sub(r"\W+", "", company.casefold())
    return any(re.sub(r"\W+", "", value.casefold()) in normalized for value in values if value)


def _country_match(location: str, strategy: dict[str, Any]) -> bool:
    if strategy.get("country_code") == "EUROPE":
        text = location.casefold()
        return any(re.search(rf"\b{re.escape(term)}\b", text) for term in EUROPE_LOCATION_TERMS)
    terms = [strategy.get("country_name", ""), strategy.get("country_code", ""), *strategy.get("cities", [])]
    text = location.casefold()
    return any(re.search(rf"\b{re.escape(str(term).casefold())}\b", text) for term in terms if term)


def evaluate_eligibility(
    job: dict[str, Any],
    preferences: dict[str, Any],
    mobility: dict[str, Any] | None = None,
    strategies: list[dict[str, Any]] | None = None,
) -> EligibilityResult:
    mobility = mobility or {}
    strategies = strategies or []
    text = " ".join(
        str(job.get(key, "")) for key in ("title", "company", "location", "remote_scope", "description")
    )
    company = str(job.get("company", ""))
    location = " ".join((str(job.get("location", "")), str(job.get("remote_scope", ""))))
    raw_metadata = job.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_signals = metadata.get("signals")
    signals: dict[str, Any] = raw_signals if isinstance(raw_signals, dict) else {}
    no_sponsor = _contains(text, NO_SPONSOR_PATTERNS)
    sponsor = not no_sponsor and (_contains(text, SPONSOR_PATTERNS) or signals.get("visa_sponsorship") is True)
    relocation = _contains(text, RELOCATION_PATTERNS) or signals.get("relocation") is True
    scoped_remote = _contains(text, SCOPED_REMOTE_PATTERNS)
    worldwide = (_contains(text, WORLDWIDE_PATTERNS) or "worldwide" in location.casefold()) and not scoped_remote
    remote = "remote" in location.casefold() or worldwide
    clearance = bool(
        re.search(r"(?:active |ability to obtain )?(?:security |secret |top secret )clearance", text, re.IGNORECASE)
    )
    excluded = [phrase for phrase in preferences.get("exclude_phrases", []) if phrase.casefold() in text.casefold()]
    blocked_company = _company_in(company, preferences.get("company_blocklist", []))

    priority = next(
        (
            strategy
            for strategy in strategies
            if strategy.get("kind") == "priority_company" and _company_in(company, strategy.get("companies", []))
        ),
        None,
    )
    country_strategy = next(
        (
            strategy
            for strategy in strategies
            if strategy.get("kind") in {"authorized_local", "relocation"} and _country_match(location, strategy)
        ),
        None,
    )
    remote_strategy = next((strategy for strategy in strategies if strategy.get("kind") == "remote"), None)
    route = str((priority or country_strategy or (remote_strategy if remote else None) or {"id": "other"})["id"])

    reasons: list[str] = []
    risks: list[str] = []
    status = EligibilityStatus.UNKNOWN
    sponsorship = "available" if sponsor else "unavailable" if no_sponsor else "unknown"
    relocation_value = "available" if relocation else "unknown"
    location_fit = "unknown"

    if blocked_company:
        status = EligibilityStatus.INELIGIBLE
        risks.append("Company is on the candidate blocklist")
    elif excluded:
        status = EligibilityStatus.INELIGIBLE
        risks.append(f"Excluded phrase: {excluded[0]}")
    elif clearance:
        status = EligibilityStatus.INELIGIBLE
        risks.append("Security clearance conflicts with the configured eligibility profile")
    elif country_strategy and country_strategy.get("kind") == "authorized_local":
        status = EligibilityStatus.ELIGIBLE
        location_fit = f"authorized:{country_strategy.get('country_code', '')}"
        reasons.append(f"Candidate has configured work authorization for {country_strategy.get('country_name')}")
    elif country_strategy and no_sponsor and country_strategy.get("requires_sponsorship"):
        status = EligibilityStatus.INELIGIBLE
        location_fit = f"sponsorship-unavailable:{country_strategy.get('country_code', '')}"
        risks.append("The role explicitly excludes sponsorship required by the configured mobility profile")
    elif country_strategy and (sponsor or relocation):
        status = EligibilityStatus.ELIGIBLE
        location_fit = f"relocation:{country_strategy.get('country_code', '')}"
        reasons.append("The target-country role explicitly supports sponsorship or relocation")
    elif worldwide and mobility.get("remote_from_current_country", False):
        status = EligibilityStatus.ELIGIBLE
        location_fit = "worldwide"
        reasons.append("Role explicitly supports worldwide remote work")
    elif remote_strategy and _country_match(location, remote_strategy):
        status = EligibilityStatus.ELIGIBLE
        location_fit = f"remote:{remote_strategy.get('country_code', '')}"
        reasons.append(f"Role explicitly includes remote work from {remote_strategy.get('country_name')}")
    elif remote:
        location_fit = "remote-scope-unknown"
        risks.append("Remote scope does not explicitly include the candidate's configured country")
    elif country_strategy:
        location_fit = f"mobility-unknown:{country_strategy.get('country_code', '')}"
        risks.append("Sponsorship and relocation support are not stated")
    elif priority:
        risks.append("Location and work authorization must be confirmed for this priority company")
    else:
        risks.append("Location eligibility could not be established")

    if sponsor:
        reasons.append("Visa sponsorship is explicitly mentioned")
    if relocation:
        reasons.append("Relocation support is explicitly mentioned")
    if priority:
        reasons.append("Company is on the candidate priority list")

    return EligibilityResult(
        status=status,
        route=route,
        sponsorship=sponsorship,
        relocation=relocation_value,
        location_fit=location_fit,
        reasons=reasons,
        risks=risks,
    )


def _candidate_terms(candidate_profile: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    skills = candidate_profile.get("skills", {})
    if isinstance(skills, dict):
        for values in skills.values():
            terms.update(str(value).casefold() for value in values)
    for section in ("summary", "headline"):
        terms.update(re.findall(r"[a-z0-9+#.]+", str(candidate_profile.get(section, "")).casefold()))
    return terms


def rule_score(
    job: dict[str, Any],
    eligibility: EligibilityResult,
    preferences: dict[str, Any],
    candidate_profile: dict[str, Any],
    strategies: list[dict[str, Any]] | None = None,
) -> ScoreResult:
    strategies = strategies or []
    title = str(job.get("title", "")).casefold()
    description = str(job.get("description", "")).casefold()
    text = f"{title} {description}"
    target_tokens = {
        token.casefold()
        for role in preferences.get("target_roles", [])
        for token in re.findall(r"[A-Za-z0-9+#.]+", role)
        if len(token) >= 3 and token.casefold() not in {"engineer", "software", "developer"}
    }
    role_hits = sorted(token for token in target_tokens if token in title)
    role_score = min(25, 10 + len(role_hits) * 5) if any(word in title for word in ("engineer", "developer")) else 5

    candidate_terms = _candidate_terms(candidate_profile)
    preferred_skills = [str(value) for value in preferences.get("preferred_skills", [])]
    if not preferred_skills:
        preferred_skills = sorted(candidate_terms)
    skill_hits = [skill for skill in preferred_skills if skill.casefold() in text and skill.casefold() in candidate_terms]
    skill_score = min(20, len(skill_hits) * 5)

    domains = [str(value) for value in preferences.get("preferred_domains", [])]
    domain_hits = [domain for domain in domains if domain.casefold() in text]
    domain_score = min(20, len(domain_hits) * 5)

    seniority_score = 7
    preferred_seniority = {str(value).casefold() for value in preferences.get("preferred_seniority", [])}
    title_seniority = next(
        (level for level in ("intern", "junior", "senior", "staff", "principal", "lead", "manager") if level in title),
        "unspecified",
    )
    if preferred_seniority:
        seniority_score = 10 if title_seniority in preferred_seniority else 4
    elif title_seniority in {"intern", "junior"}:
        seniority_score = 3

    location_score = {
        EligibilityStatus.ELIGIBLE: 15,
        EligibilityStatus.UNKNOWN: 8,
        EligibilityStatus.INELIGIBLE: 0,
    }[eligibility.status]
    salary_score = 5
    if job.get("salary_min") or job.get("salary_max"):
        salary_score = 10
        salary = preferences.get("salary", {})
        minimum = salary.get("minimum") if isinstance(salary, dict) else None
        currency = salary.get("currency") if isinstance(salary, dict) else ""
        if minimum and currency and str(job.get("salary_currency", "")).casefold() == str(currency).casefold():
            if float(job.get("salary_max") or job.get("salary_min") or 0) < float(minimum):
                salary_score = 0

    dimensions = {
        "role_domain": role_score,
        "stack": skill_score,
        "domain_experience": domain_score,
        "seniority": seniority_score,
        "location_authorization": location_score,
        "salary_employment": salary_score,
    }
    total = sum(dimensions.values())
    strategy = next((item for item in strategies if item.get("id") == eligibility.route), None)
    if strategy and strategy.get("kind") == "priority_company":
        total = max(total, 55)
    if eligibility.status == EligibilityStatus.INELIGIBLE:
        total = min(total, 39)
    threshold = int(strategy.get("threshold", 80)) if strategy else 80
    verdict = (
        "reject"
        if eligibility.status == EligibilityStatus.INELIGIBLE
        else "review"
        if total >= threshold
        else "low_priority"
    )
    evidence: list[dict[str, str]] = []
    if skill_hits:
        evidence.append({"requirement": "Relevant skills", "profile_evidence": ", ".join(skill_hits)})
    if domain_hits:
        evidence.append({"requirement": "Preferred domain", "profile_evidence": ", ".join(domain_hits)})
    if eligibility.reasons:
        evidence.append({"requirement": "Location and authorization", "profile_evidence": eligibility.reasons[0]})
    gaps = [
        {"requirement": risk, "severity": "high" if eligibility.status == EligibilityStatus.INELIGIBLE else "medium"}
        for risk in eligibility.risks
    ]
    return ScoreResult(
        total=total,
        dimensions=dimensions,
        confidence=0.65 if eligibility.status != EligibilityStatus.UNKNOWN else 0.5,
        verdict=verdict,
        evidence=evidence,
        gaps=gaps,
        provider="rules",
        model="deterministic-v2",
        prompt_version=f"{SCORING_PROMPT_VERSION}:rules",
    )
