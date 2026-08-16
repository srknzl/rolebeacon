from __future__ import annotations

import re
from typing import Any

from .domain import EligibilityResult, EligibilityStatus, ScoreResult

SCORING_PROMPT_VERSION = "job-fit-v10"

# Ineligibility is a hard gate: no combination of fit signals may push a total above this cap.
# LLM scoring is only ever invoked for eligible jobs (see sync.py), so every ineligible job's
# score is this rule-based score, capped here - the cap always applies regardless of provider.
INELIGIBLE_SCORE_CAP = 39

# Every ScoreResult (rules in this file, or the LLM in llm.py) fills these six dimension keys
# with a point total up to the max below - the LLM prompt's SCORING_RUBRIC uses the same maxima,
# so this is the one place both providers' points mean the same thing to a reader. Drives the
# "why this score" drilldown on the job detail page: label, ceiling, and what earns the points.
DIMENSION_META: list[tuple[str, str, int, str]] = [
    ("role_domain", "Role match", 30, "How closely the title matches your target roles."),
    ("stack", "Skills", 20, "Your skills mentioned in the posting text."),
    ("domain_experience", "Domain experience", 10, "Your preferred domains mentioned in the posting text."),
    ("seniority", "Seniority", 15, "Title seniority against your preferred seniority."),
    ("location_authorization", "Location & authorization", 15, "Eligibility status: eligible, unknown, or ineligible."),
    ("salary_employment", "Salary & employment", 10, "Whether stated pay meets your minimum, when the posting states one."),
]
DIMENSION_MAXIMUMS: dict[str, int] = {key: max_points for key, _, max_points, _ in DIMENSION_META}

# Words that end a skill phrase rather than belong to it, so "5 years of Java experience" and
# "5+ years of experience with Java" both resolve to the skill "Java", not "Java experience", and
# "5 years of Go required" resolves to "Go", not "Go required". The rest are generic filler that
# real postings put right where a skill would go ("5+ years of non-internship professional
# software development experience", "an advanced degree", "one or more languages", "state of the
# art") - never a skill name themselves, tuned against real live-data false positives.
_REQUIREMENT_STOPWORDS = (
    "experience", "background", "development", "skills", "skill", "knowledge", "and", "or", "with",
    "required", "preferred", "needed", "is", "are", "a", "the", "in", "of", "an", "to", "into",
    "one", "non", "full", "software", "professional", "advanced", "expert", "hands", "state", "progressive",
    "relevant", "industry", "recent", "overall", "technical",
)
# Never itself a skill - it's the verb before the real (unextracted) object, as in "years of
# experience leading design" or "years of experience building large-scale infrastructure".
# ponytail: blocks every -ing word, including real skill nouns like "Networking" or the second
# word of "Data Engineering" - degrades those to a shorter/partial skill rather than dropping the
# match, which fits "tolerate misses, avoid bogus captures." Allowlist a specific term here if one
# needs to survive whole.
_SKILL_WORD = r"(?:(?!(?:" + "|".join(_REQUIREMENT_STOPWORDS) + r")\b)(?!\w+ing\b)[A-Za-z][\w+#.]{1,24})"
# Heuristic, single-pass "N years of X" extraction - not a grammar. Misses are fine; a skill
# phrase that accidentally swallows a stray word is the acceptable failure mode here. The
# lookbehind stops a two-word skill from crossing a sentence boundary ("Kafka. General" in
# "...with Kafka. General software...") since a real skill name never ends a matched word in ".".
# At least one of "of"/"experience with|in|using" must be present - both fully optional used to
# let bare "N years <two random words>" match any unrelated "N years" mention at all, e.g. "10
# years to exercise your options" (stock-plan clause) or "5-10 years into the future".
_EXPERIENCE_PATTERN = re.compile(
    rf"\b(\d+)\+?\s*years?\s+(?:of\s+experience\s+(?:with|in|using)\s+|of\s+|experience\s+(?:with|in|using)\s+)"
    rf"({_SKILL_WORD}(?<!\.)(?:[\s/-]+{_SKILL_WORD}){{0,1}})",
    re.IGNORECASE,
)


def extract_experience_requirements(description: str) -> list[dict[str, Any]]:
    """Deterministic, LLM-free 'N years of X' extraction so rules-only mode has the same
    experience-requirement signal as LLM mode. Keeps the longest years figure seen per skill."""
    found: dict[str, tuple[str, int]] = {}
    for match in _EXPERIENCE_PATTERN.finditer(description):
        years = int(match.group(1))
        skill = match.group(2).strip().rstrip(".,")
        if not skill or not (0 < years <= 40):
            continue
        key = skill.casefold()
        if key not in found or years > found[key][1]:
            found[key] = (skill, years)
    return [{"skill": skill, "years": years} for skill, years in found.values()]

# A title's head noun is the job. "Engineering Manager", "Sales Engineer", and
# "Customer Engineer (Pre-Sales)" all contain an engineering word while being a different
# job, so these are tested first and a match rules the engineering family out.
OTHER_ROLE_FAMILY_TERMS = (
    "manager", "director", "head of", "chief", "vice president", "product owner",
    "designer", "recruiter", "recruiting", "sourcer", "sales", "account executive",
    "customer success", "customer engineer", "solutions architect", "solutions engineer",
    "sales engineer", "support engineer", "consultant", "analyst", "marketing",
    "technical writer", "instructor", "teacher", "accountant", "counsel", "paralegal",
)
# Titles that do the work, whatever the specialism in front of them.
ENGINEERING_ROLE_TERMS = ("engineer", "developer", "programmer", "architect", "sre", "devops")
# Words every target role shares, so they cannot tell one target role from another.
GENERIC_ROLE_WORDS = frozenset(
    {
        "engineer", "engineering", "developer", "development", "software", "programmer",
        "senior", "staff", "principal", "lead", "junior", "mid", "level", "the", "and",
    }
)

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
    code = str(strategy.get("country_code", "")).strip()
    # A short ISO code is only trustworthy as an exact-case token (e.g. "Berlin, DE"). Casefolding it
    # like the name/city terms below produces false positives, e.g. "de" inside "Île-de-France" matching
    # country_code "DE" (Germany) for an unrelated French location.
    if code and re.search(rf"\b{re.escape(code)}\b", location):
        return True
    terms = [strategy.get("country_name", ""), *strategy.get("cities", [])]
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
            if strategy.get("kind") in ("priority_company", "company_watchlist")
            and _company_in(company, strategy.get("companies", []))
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
        label = "watchlist" if priority.get("kind") == "company_watchlist" else "priority"
        risks.append(f"Location and work authorization must be confirmed for this {label} company")
    else:
        risks.append("Location eligibility could not be established")

    if sponsor:
        reasons.append("Visa sponsorship is explicitly mentioned")
    if relocation:
        reasons.append("Relocation support is explicitly mentioned")
    if priority:
        reasons.append(
            "Company is on the candidate watchlist"
            if priority.get("kind") == "company_watchlist"
            else "Company is on the candidate priority list"
        )

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


def _role_match(title: str, preferences: dict[str, Any]) -> tuple[int, bool]:
    """Score the title against the roles asked for, and say whether it is the same job at all.

    A different job family is not a weak match, it is the wrong job: an engineering manager,
    a product manager, and a pre-sales engineer all mention engineering while doing none of it.
    Inside the family the title only has to be an engineering title, because platform, backend,
    and distributed-systems work is the same work under different names. A term the candidate
    put in their own target roles never disqualifies anything.
    """
    targets = [str(role).casefold() for role in preferences.get("target_roles", []) if str(role).strip()]
    wanted = " ".join(targets)
    specifics = {
        word
        for role in targets
        for word in re.findall(r"[a-z0-9+#.]+", role)
        if len(word) >= 3 and word not in GENERIC_ROLE_WORDS
    }
    if any(term in title for term in OTHER_ROLE_FAMILY_TERMS if term not in wanted):
        return 2, False
    if not any(term in title for term in ENGINEERING_ROLE_TERMS) and not any(word in title for word in specifics):
        return 6, False
    if any(role in title for role in targets):
        return 30, True
    return min(30, 17 + 5 * len([word for word in specifics if word in title])), True


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
    role_score, same_role_family = _role_match(title, preferences)

    candidate_terms = _candidate_terms(candidate_profile)
    preferred_skills = [str(value) for value in preferences.get("preferred_skills", [])]
    if not preferred_skills:
        preferred_skills = sorted(candidate_terms)
    skill_hits = [skill for skill in preferred_skills if skill.casefold() in text and skill.casefold() in candidate_terms]
    skill_score = min(20, len(skill_hits) * 5)

    domains = [str(value) for value in preferences.get("preferred_domains", [])]
    domain_hits = [domain for domain in domains if domain.casefold() in text]
    domain_score = min(10, len(domain_hits) * 5)

    seniority_score = 10
    preferred_seniority = {str(value).casefold() for value in preferences.get("preferred_seniority", [])}
    title_seniority = next(
        (level for level in ("intern", "junior", "senior", "staff", "principal", "lead", "manager") if level in title),
        "unspecified",
    )
    if preferred_seniority:
        seniority_score = 15 if title_seniority in preferred_seniority else 6
    elif title_seniority in {"intern", "junior"}:
        seniority_score = 4

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
    strategy = next((item for item in strategies if item.get("id") == eligibility.route), None)
    # A priority/watchlist company is a reason to look at its engineering roles, not at its
    # procurement or pre-sales openings, and the floor never touches the title match
    # itself because that dimension is the answer to "is this my job?". Watchlist gets a
    # smaller floor than priority - "lighter weight" is the field's own documented intent.
    if strategy and same_role_family:
        if strategy.get("kind") == "priority_company":
            _raise_dimensions_to_floor(dimensions, 55)
        elif strategy.get("kind") == "company_watchlist":
            _raise_dimensions_to_floor(dimensions, 45)
    if eligibility.status == EligibilityStatus.INELIGIBLE:
        _cap_dimensions(dimensions, INELIGIBLE_SCORE_CAP)
    total = sum(dimensions.values())
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
    for requirement in extract_experience_requirements(str(job.get("description", ""))):
        if requirement["skill"].casefold() not in candidate_terms:
            gaps.append(
                {
                    "requirement": f"Posting asks for {requirement['years']}+ years of {requirement['skill']}, "
                    "not found in your profile",
                    "severity": "medium",
                }
            )
    if not same_role_family:
        gaps.insert(0, {"requirement": f"{job.get('title', 'This title')} is a different role from your targets", "severity": "high"})
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


def _raise_dimensions_to_floor(dimensions: dict[str, int], floor: int) -> None:
    shortfall = max(0, floor - sum(dimensions.values()))
    for key in ("domain_experience", "stack"):
        increase = min(shortfall, DIMENSION_MAXIMUMS[key] - dimensions[key])
        dimensions[key] += increase
        shortfall -= increase
        if not shortfall:
            return


def _cap_dimensions(dimensions: dict[str, int], cap: int) -> None:
    overflow = max(0, sum(dimensions.values()) - cap)
    for key in ("salary_employment", "location_authorization", "seniority", "domain_experience", "stack", "role_domain"):
        decrease = min(overflow, dimensions[key])
        dimensions[key] -= decrease
        overflow -= decrease
        if not overflow:
            return
