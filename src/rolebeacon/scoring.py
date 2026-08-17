from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from .domain import EligibilityResult, EligibilityStatus, ScoreResult
from .profile import CONTINENT_COUNTRY_CODES, DEFAULT_SCORE_WEIGHTS, country_names_by_code

SCORING_PROMPT_VERSION = "job-fit-v15"

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
DIMENSION_MAXIMUMS: dict[str, int] = dict(DEFAULT_SCORE_WEIGHTS)

# The only place location_authorization is scored, for both providers - it is a pure lookup from
# the deterministic eligibility gate, never a model judgment call. rule_score uses it directly;
# llm.py's _normalize_score splices it into the model's dimensions before summing.
# Recognized job-title seniority buckets, in priority order (first match wins when a title
# names more than one, e.g. "Mid to Senior Engineer"). This is also the seniority picker's
# vocabulary (see seniority_level_options) - a preference can only ever match a level this
# extraction actually produces, so "mid" needs its own word-boundary check: an unguarded
# substring match would fire on "Midwest", "Middleware", or "amid".
SENIORITY_LEVELS: tuple[str, ...] = ("intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager")
_SENIORITY_LEVEL_LABELS = {
    "intern": "Intern",
    "junior": "Junior",
    "mid": "Mid-level",
    "senior": "Senior",
    "staff": "Staff",
    "principal": "Principal",
    "lead": "Lead",
    "manager": "Manager",
}
_MID_SENIORITY_PATTERN = re.compile(r"\bmid\b")


def seniority_level_options() -> list[dict[str, str]]:
    return [{"code": level, "label": _SENIORITY_LEVEL_LABELS[level]} for level in SENIORITY_LEVELS]


def _title_seniority(title: str) -> str:
    for level in SENIORITY_LEVELS:
        if level == "mid":
            if _MID_SENIORITY_PATTERN.search(title):
                return level
        elif level in title:
            return level
    return "unspecified"


LOCATION_SCORES: dict[EligibilityStatus, int] = {
    EligibilityStatus.ELIGIBLE: 15,
    EligibilityStatus.UNKNOWN: 8,
    EligibilityStatus.INELIGIBLE: 0,
}


def configured_score_weights(preferences: dict[str, Any] | None = None) -> dict[str, int]:
    raw = (preferences or {}).get("score_weights")
    if not isinstance(raw, dict) or set(raw) != set(DEFAULT_SCORE_WEIGHTS):
        return dict(DEFAULT_SCORE_WEIGHTS)
    values = {key: int(value) for key, value in raw.items()}
    return values if all(value >= 0 for value in values.values()) and sum(values.values()) == 100 else dict(DEFAULT_SCORE_WEIGHTS)


def scoring_behavior_version(preferences: dict[str, Any] | None = None) -> str:
    """Stable suffix that requeues evaluations once when the point distribution changes."""
    encoded = json.dumps(configured_score_weights(preferences), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:12]


def dimension_metadata(preferences: dict[str, Any] | None = None) -> list[tuple[str, str, int, str]]:
    weights = configured_score_weights(preferences)
    return [(key, label, weights[key], hint) for key, label, _maximum, hint in DIMENSION_META]


def apply_score_weights(dimensions: dict[str, int], preferences: dict[str, Any]) -> dict[str, int]:
    weights = configured_score_weights(preferences)
    return {
        key: round(int(value) * weights[key] / DEFAULT_SCORE_WEIGHTS[key])
        for key, value in dimensions.items()
    }


def compute_verdict(status: EligibilityStatus, total: int, threshold: int) -> str:
    """The one verdict rule shared by rule_score and llm.py's _normalize_score."""
    return (
        "reject"
        if status == EligibilityStatus.INELIGIBLE
        else "review"
        if total >= threshold
        else "low_priority"
    )

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
# Never itself a skill - it's the verb before the real object, as in "years of experience
# providing technical leadership". A short allowlist of connector verbs (below, in
# _EXPERIENCE_PATTERN) lets the object after "building"/"developing"/"designing"/"leading" through
# instead; every other -ing verb still blocks the match outright rather than exposing it.
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
# years to exercise your options" (stock-plan clause) or "5-10 years into the future". Optional
# apostrophe after "years" covers the formal "5 years' experience with X" phrasing. The extra
# "experience building|developing|working with|designing|leading" connectors catch postings that
# never write the bare "of X"/"with X" shape at all, e.g. "years of experience building X".
_EXPERIENCE_PATTERN = re.compile(
    rf"\b(\d+)\+?\s*years?['’]?\s+"
    rf"(?:of\s+experience\s+(?:with|in|using|building|developing|working\s+with|designing|leading)\s+|of\s+|experience\s+(?:with|in|using)\s+)"
    rf"({_SKILL_WORD}(?<!\.)(?:[\s/-]+{_SKILL_WORD}){{0,1}})",
    re.IGNORECASE,
)


def _is_plausible_skill(skill: str, known_terms: set[str]) -> bool:
    """A captured 'N years of X' phrase is kept only if X reads like an actual skill name: a
    multi-word phrase or one containing a symbol ("data structures", "C++", ".NET", "Node.js"),
    capitalized in the source text (real tech nouns like "Kafka"/"React" are; filler words like
    "modern"/"quota"/"related" never are, mid-sentence), or already in the candidate's own
    vocabulary. Drops the single-lowercase-word junk the stopword list alone doesn't catch."""
    if re.search(r"[^A-Za-z]", skill):
        return True
    if skill[:1].isupper():
        return True
    return skill.casefold() in known_terms


def extract_experience_requirements(description: str, known_terms: set[str] | None = None) -> list[dict[str, Any]]:
    """Deterministic, LLM-free 'N years of X' extraction so rules-only mode has the same
    experience-requirement signal as LLM mode. Keeps the longest years figure seen per skill.
    known_terms is the candidate's own vocabulary (see candidate_terms()): it both filters out
    implausible captures that aren't in it and don't look like a skill name, and marks each
    surviving requirement "unmet" when the candidate's profile doesn't already show it."""
    known_terms = known_terms or set()
    found: dict[str, tuple[str, int]] = {}
    for match in _EXPERIENCE_PATTERN.finditer(description):
        years = int(match.group(1))
        skill = match.group(2).strip().rstrip(".,")
        if not skill or not (0 < years <= 40) or not _is_plausible_skill(skill, known_terms):
            continue
        key = skill.casefold()
        if key not in found or years > found[key][1]:
            found[key] = (skill, years)
    return [
        {"skill": skill, "years": years, "unmet": skill.casefold() not in known_terms}
        for skill, years in found.values()
    ]

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
    r"visa sponsorship (?:is |are )?(?:available|provided|offered)",
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
    r"hire(?:s|d|ing)?(?: people| talent| employees)? (?:from )?anywhere in the world",
)
# A bare "N days/weeks/months per year" right after a worldwide-remote claim means the claim is a
# bounded work-from-abroad perk on an otherwise onsite/hybrid role (e.g. GetYourGuide's "Work from
# anywhere in the world for 30 days per year" alongside a 3-day-a-week office requirement), not the
# job's actual remote eligibility - it must not satisfy WORLDWIDE_PATTERNS.
_TIME_BOXED_WORLDWIDE_QUALIFIER = re.compile(r"\d+\s*(?:days?|weeks?|months?)\s*(?:per|a|each)\s*year", re.IGNORECASE)
SCOPED_REMOTE_PATTERNS = (
    r"(?:your|the) country of employment",
    r"anywhere in(?:side)?[- ]country",
    r"within (?:your|the) (?:country|region)",
    r"remote (?:within|in|across) [a-z][a-z .-]+",
    r"must be (?:based|located|resident) in",
)
# Calibration knobs for the two places official ISO/pycountry names don't match how a job posting
# writes a location: a country referred to by a constituent nation/alternate name, and a region
# referred to by the bloc's own name rather than any member country's name.
COUNTRY_LOCATION_ALIASES: dict[str, tuple[str, ...]] = {
    "CZ": ("czech republic",),
    "GB": ("united kingdom", "great britain", "britain", "england", "scotland", "wales", "uk"),
    "KR": ("south korea", "republic of korea"),
    "TR": ("türkiye", "turkiye", "turkey"),
    "US": ("united states", "united states of america", "usa"),
}
REGION_LOCATION_ALIASES: dict[str, tuple[str, ...]] = {
    "EUROPE": ("europe", "european union", "eu", "eea"),
    # Region-member expansion deliberately never matches a bare 2-letter ISO code (see
    # _country_match) to avoid "IT"/"NO"-style false positives in all-caps text, but real US
    # postings overwhelmingly write "US"/"USA", not "United States" - so, like EUROPE's "eu"
    # above, curate the dominant real-world abbreviations as an explicit region alias instead.
    "NORTH_AMERICA": ("north america", "us", "usa"),
}


def _contains(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _contains_unbounded_worldwide_claim(text: str) -> bool:
    """Like _contains(text, WORLDWIDE_PATTERNS), but a match immediately followed by a
    days/weeks/months-per-year qualifier is a capped travel perk, not a worldwide-remote claim."""
    for pattern in WORLDWIDE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            trailing = text[match.end():match.end() + 40]
            if not _TIME_BOXED_WORLDWIDE_QUALIFIER.search(trailing):
                return True
    return False


def _company_in(company: str, values: list[str]) -> bool:
    legal_suffixes = {
        "ab", "ag", "as", "bv", "co", "company", "corp", "corporation", "gmbh", "inc",
        "incorporated", "limited", "llc", "ltd", "nv", "oy", "plc", "sa", "ş",
    }

    def normalized(value: str) -> tuple[str, ...]:
        folded = unicodedata.normalize("NFKC", value).casefold()
        tokens = re.findall(r"[^\W_]+", folded, re.UNICODE)
        while tokens and tokens[-1] in legal_suffixes:
            tokens.pop()
        return tuple(tokens)

    def contains_tokens(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
        return any(longer[index : index + len(shorter)] == shorter for index in range(len(longer) - len(shorter) + 1))

    company_name = normalized(company)
    for value in values:
        configured_name = normalized(value)
        if not company_name or not configured_name:
            continue
        if contains_tokens(company_name, configured_name) or contains_tokens(configured_name, company_name):
            return True
    return False


_CLEARANCE_MENTION = re.compile(
    r"\b(?:security[- ]clearance|public[- ]trust|ts/sci|top[- ]secret|secret[- ]clearance)\b",
    re.IGNORECASE,
)
_CLEARANCE_NEGATED = re.compile(
    r"\b(?:no|not|does not|doesn't|without)\b.{0,45}\b(?:security[- ]clearance|clearance|public[- ]trust|ts/sci)\b"
    r"|\b(?:security[- ]clearance|clearance|public[- ]trust|ts/sci)\b.{0,45}\b(?:not required|isn't required)\b",
    re.IGNORECASE,
)
_CLEARANCE_PREFERRED = re.compile(
    r"\b(?:security[- ]clearance|clearance|public[- ]trust|ts/sci)\b.{0,35}\b(?:preferred|nice to have|a plus|not required)\b",
    re.IGNORECASE,
)
_CLEARANCE_OBTAIN = re.compile(
    r"\b(?:ability|able|eligible|willingness|required) to (?:obtain|maintain)\b.{0,45}\b(?:clearance|public[- ]trust)\b"
    r"|\b(?:clearance|public[- ]trust)\b.{0,45}\b(?:must|required to|ability to) (?:be )?(?:obtain|obtained|maintain)",
    re.IGNORECASE,
)
_CLEARANCE_ACTIVE = re.compile(
    r"\b(?:active|current|existing)\b.{0,35}\b(?:clearance|ts/sci|top[- ]secret|secret)\b"
    r"|\b(?:ts/sci|top[- ]secret|secret[- ]clearance|security[- ]clearance|cleared candidate)\b.{0,35}\b(?:required|must have|needed)\b"
    r"|\b(?:required|must have|needed)\b.{0,35}\b(?:ts/sci|top[- ]secret|secret[- ]clearance|security[- ]clearance)\b",
    re.IGNORECASE,
)


def clearance_requirements(text: str) -> list[dict[str, str]]:
    """Classify explicit English clearance wording while preserving its exact sentence."""
    results: list[dict[str, str]] = []
    for sentence in (part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()):
        if not _CLEARANCE_MENTION.search(sentence):
            continue
        kind = (
            "preferred" if _CLEARANCE_PREFERRED.search(sentence)
            else "not_required" if _CLEARANCE_NEGATED.search(sentence)
            else "ability_to_obtain" if _CLEARANCE_OBTAIN.search(sentence)
            else "active_required" if _CLEARANCE_ACTIVE.search(sentence)
            else "ambiguous"
        )
        level = "TS/SCI" if re.search(r"\bTS/SCI\b", sentence, re.IGNORECASE) else (
            "Top Secret" if re.search(r"\btop[- ]secret\b", sentence, re.IGNORECASE) else
            "Secret" if re.search(r"\bsecret\b", sentence, re.IGNORECASE) else ""
        )
        jurisdiction = "US" if re.search(r"\b(?:US|U\.S\.|United States|DoD)\b", sentence) else ""
        results.append({"kind": kind, "evidence": sentence, "level": level, "jurisdiction": jurisdiction})
    return results


def _matching_active_clearance(policy: dict[str, Any], requirement: dict[str, str]) -> bool:
    credentials = policy.get("credentials", [])
    if not isinstance(credentials, list):
        return False
    for credential in credentials:
        if not isinstance(credential, dict) or credential.get("status") != "active":
            continue
        jurisdiction = requirement.get("jurisdiction", "")
        level = requirement.get("level", "")
        if jurisdiction and str(credential.get("jurisdiction", "")).casefold() != jurisdiction.casefold():
            continue
        if level and str(credential.get("level", "")).casefold() != level.casefold():
            continue
        return True
    return False


# A US state's postal abbreviation is, by pure coincidence, also a real ISO country code for dozens
# of states (CA/Canada, IN/India, GA/Georgia, DE/Germany, PA/Panama, ...). A bare-code location match
# below is genuinely ambiguous only in that case, and a US postal abbreviation only ever shows up
# alongside an explicit "United States"/"USA"/"US" - so once the location names the US, a bare match
# for any other code is that collision, not a real country hit.
_US_LOCATION_MARKERS = (*COUNTRY_LOCATION_ALIASES["US"], "us")


def _names_united_states(location: str) -> bool:
    return any(re.search(rf"\b{re.escape(marker)}\b", location, re.IGNORECASE) for marker in _US_LOCATION_MARKERS)


def _place_match(location: str, code: str, name: str, cities: list[str] | tuple[str, ...] = ()) -> bool:
    code = str(code).strip()
    # A short ISO code is only trustworthy as an exact-case token (e.g. "Berlin, DE"). Casefolding it
    # like the name/city terms below produces false positives, e.g. "de" inside "Île-de-France" matching
    # country_code "DE" (Germany) for an unrelated French location. See _names_united_states for the
    # other collision this guards against: a US state postal abbreviation matching an unrelated code.
    if code and re.search(rf"\b{re.escape(code)}\b", location) and (code.upper() == "US" or not _names_united_states(location)):
        return True
    terms = [name, *COUNTRY_LOCATION_ALIASES.get(code.upper(), ()), *cities]
    text = location.casefold()
    return any(re.search(rf"\b{re.escape(str(term).casefold())}\b", text) for term in terms if term)


def _country_match(location: str, strategy: dict[str, Any]) -> bool:
    code = str(strategy.get("country_code", "")).strip().upper()
    if code in CONTINENT_COUNTRY_CODES:
        text = location.casefold()
        if any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in REGION_LOCATION_ALIASES.get(code, ())):
            return True
        # A region match is name/alias only, never a bare 2-letter code - "IT"/"NO" inside an
        # all-caps location string is a false-positive generator once dozens of member codes are
        # in play, unlike a single explicitly-configured country (_place_match's own code check).
        names = country_names_by_code()
        return any(_place_match(location, "", names.get(member, ""), ()) for member in CONTINENT_COUNTRY_CODES[code])
    return _place_match(location, code, str(strategy.get("country_name", "")), strategy.get("cities", []))


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
    worldwide = (
        _contains_unbounded_worldwide_claim(text) or "worldwide" in location.casefold()
    ) and not scoped_remote
    remote = "remote" in location.casefold() or worldwide
    clearance = clearance_requirements(text)
    raw_clearance_policy = mobility.get("clearance_policy")
    clearance_policy: dict[str, Any] = raw_clearance_policy if isinstance(raw_clearance_policy, dict) else {}
    excluded = [phrase for phrase in preferences.get("exclude_phrases", []) if phrase.casefold() in text.casefold()]
    blocked_company = _company_in(company, preferences.get("company_blocklist", []))
    policy_exclusions = [
        phrase for phrase in clearance_policy.get("explicitly_excluded_requirements", [])
        if str(phrase).strip() and str(phrase).casefold() in text.casefold()
    ]
    gating_clearance = next(
        (item for item in clearance if item["kind"] in {"active_required", "ability_to_obtain"}),
        None,
    )
    matched_clearance = bool(
        gating_clearance
        and gating_clearance["kind"] == "active_required"
        and _matching_active_clearance(clearance_policy, gating_clearance)
    )
    if matched_clearance:
        gating_clearance = None
    salary_preference = preferences.get("salary", {})
    salary_hard_conflict = False
    if isinstance(salary_preference, dict) and salary_preference.get("hard_filter"):
        preferred_currency = str(salary_preference.get("currency", "")).casefold()
        job_currency = str(job.get("salary_currency", "")).casefold()
        minimum = salary_preference.get("minimum")
        maximum = job.get("salary_max") or job.get("salary_min")
        salary_hard_conflict = bool(
            minimum is not None and maximum is not None and preferred_currency and job_currency == preferred_currency
            and float(maximum) < float(minimum)
        )

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
    # remote_strategy only earns the route if its own country actually matches this job's remote
    # scope - otherwise a job remote-scoped to a different country (e.g. "Remote, United States"
    # for a candidate whose remote strategy is "remote-from-tr") wrongly took on the candidate's
    # own remote-strategy id despite never having been checked against it.
    remote_route_strategy = remote_strategy if remote and _country_match(location, remote_strategy or {}) else None
    route = str((priority or country_strategy or remote_route_strategy or {"id": "other"})["id"])
    governing_strategy = next((item for item in strategies if item.get("id") == route), None)
    threshold = int(governing_strategy.get("threshold", 80)) if governing_strategy else 80

    reasons: list[str] = []
    risks: list[str] = []
    status = EligibilityStatus.UNKNOWN
    sponsorship = "available" if sponsor else "unavailable" if no_sponsor else "unknown"
    relocation_value = "available" if relocation else "unknown"
    location_fit = "unknown"
    if matched_clearance:
        reasons.append("A matching active clearance is recorded in the local mobility profile")

    if blocked_company:
        status = EligibilityStatus.INELIGIBLE
        risks.append("Company is on the candidate blocklist")
    elif excluded:
        status = EligibilityStatus.INELIGIBLE
        risks.append(f"Excluded phrase: {excluded[0]}")
    elif policy_exclusions:
        status = EligibilityStatus.INELIGIBLE
        risks.append(f"Rejected by your excluded clearance requirement: {policy_exclusions[0]}")
    elif salary_hard_conflict:
        status = EligibilityStatus.INELIGIBLE
        risks.append("The posting's stated comparable salary is below your configured hard minimum")
    elif gating_clearance and (
        clearance_policy.get("status") == "cannot_meet"
        or clearance_policy.get("willing_to_undergo_vetting") is False
    ):
        status = EligibilityStatus.INELIGIBLE
        risks.append(
            f"Posting clearance requirement conflicts with your explicit policy: {gating_clearance['evidence']}"
        )
    elif gating_clearance:
        status = EligibilityStatus.UNKNOWN
        risks.append(
            f"Clearance requirement needs verification against your profile: {gating_clearance['evidence']}"
        )
    elif country_strategy and country_strategy.get("kind") == "authorized_local":
        status = EligibilityStatus.ELIGIBLE
        location_fit = f"authorized:{country_strategy.get('country_code', '')}"
        reasons.append(f"Candidate has configured work authorization for {country_strategy.get('country_name')}")
    elif country_strategy and no_sponsor and country_strategy.get("requires_sponsorship"):
        status = EligibilityStatus.INELIGIBLE
        location_fit = f"sponsorship-unavailable:{country_strategy.get('country_code', '')}"
        risks.append("The role explicitly excludes sponsorship required by the configured mobility profile")
    elif country_strategy and country_strategy.get("requires_sponsorship") and sponsor:
        status = EligibilityStatus.ELIGIBLE
        location_fit = f"sponsorship:{country_strategy.get('country_code', '')}"
        reasons.append("The target-country role explicitly supports required visa sponsorship")
    elif country_strategy and country_strategy.get("requires_sponsorship") and not remote:
        location_fit = f"sponsorship-unknown:{country_strategy.get('country_code', '')}"
        risks.append("Visa sponsorship required by the configured mobility profile is not confirmed")
    elif country_strategy and relocation:
        status = EligibilityStatus.ELIGIBLE
        location_fit = f"relocation:{country_strategy.get('country_code', '')}"
        reasons.append("The target-country role explicitly supports relocation")
    elif worldwide and mobility.get("remote_from_current_country", False):
        status = EligibilityStatus.ELIGIBLE
        location_fit = "worldwide"
        reasons.append("Role explicitly supports worldwide remote work")
    elif remote_strategy and _country_match(location, remote_strategy):
        status = EligibilityStatus.ELIGIBLE
        location_fit = f"remote:{remote_strategy.get('country_code', '')}"
        reasons.append(f"Role explicitly includes remote work from {remote_strategy.get('country_name')}")
    elif remote and country_strategy:
        # Remote, but explicitly scoped to a country other than the candidate's own (matched a
        # relocation/authorized_local strategy above, just not one of the sponsor/relocation/
        # authorized branches) - a more specific, honest label than the generic case below.
        location_fit = f"remote-scoped:{country_strategy.get('country_code', '')}"
        risks.append(f"Remote scope may be limited to {country_strategy.get('country_name', 'a specific country')}, not stated as worldwide")
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
    preferred_clearance = next((item for item in clearance if item["kind"] == "preferred"), None)
    if preferred_clearance:
        risks.append(f"Clearance is preferred, not required: {preferred_clearance['evidence']}")

    return EligibilityResult(
        status=status,
        route=route,
        sponsorship=sponsorship,
        relocation=relocation_value,
        location_fit=location_fit,
        reasons=reasons,
        risks=risks,
        threshold=threshold,
    )


def location_requirement(location_fit: str) -> str:
    """One plain sentence for what location_fit means, for display next to a job. Kept beside the
    evaluate_eligibility branches that produce each value so the two prefixes can't drift apart."""
    prefix, _, code = location_fit.partition(":")
    name = country_names_by_code().get(code, code)
    sentence = {
        "authorized": f"You are already authorized to work in {name}.",
        "sponsorship-unavailable": f"Would need sponsorship in {name}, which the posting excludes.",
        "sponsorship": f"The posting explicitly supports required visa sponsorship in {name}.",
        "sponsorship-unknown": f"Would need sponsorship in {name}, but the posting does not confirm it.",
        "relocation": f"The posting explicitly supports relocation to {name}; visa sponsorship is evaluated separately.",
        "worldwide": "The posting explicitly supports worldwide remote work.",
        "remote": f"The posting explicitly includes remote work from {name}.",
        "remote-scoped": f"Remote, but appears scoped to {name} rather than worldwide.",
        "remote-scope-unknown": "Remote, but the posting does not state which countries are included.",
        "mobility-unknown": f"Targets {name}, but sponsorship and relocation support are not stated.",
    }.get(prefix)
    return sentence or "Location requirement could not be determined from the posting."


def candidate_terms(candidate_profile: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    skills = candidate_profile.get("skills", {})
    if isinstance(skills, dict):
        for values in skills.values():
            terms.update(str(value).casefold() for value in values)
    evidence_sections = {
        "summary": candidate_profile.get("summary", ""),
        "headline": candidate_profile.get("headline", ""),
        "experience": candidate_profile.get("experience", []),
        "projects": candidate_profile.get("projects", []),
        "education": candidate_profile.get("education", []),
        "languages": candidate_profile.get("languages", []),
    }
    for value in evidence_sections.values():
        terms.update(re.findall(r"[a-z0-9+#.]+", str(value).casefold()))
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

    known_terms = candidate_terms(candidate_profile)
    preferred_skills = [str(value) for value in preferences.get("preferred_skills", [])]
    if not preferred_skills:
        preferred_skills = sorted(known_terms)
    skill_hits = [skill for skill in preferred_skills if skill.casefold() in text and skill.casefold() in known_terms]
    skill_score = min(20, len(skill_hits) * 5)

    domains = [str(value) for value in preferences.get("preferred_domains", [])]
    domain_hits = [domain for domain in domains if domain.casefold() in text]
    domain_score = min(10, len(domain_hits) * 5)

    seniority_score = 10
    preferred_seniority = {str(value).casefold() for value in preferences.get("preferred_seniority", [])}
    title_seniority = _title_seniority(title)
    if preferred_seniority:
        seniority_score = 15 if title_seniority in preferred_seniority else 6
    elif title_seniority in {"intern", "junior"}:
        seniority_score = 4

    location_score = LOCATION_SCORES[eligibility.status]
    salary_score = 5
    if job.get("salary_min") or job.get("salary_max"):
        salary = preferences.get("salary", {})
        minimum = salary.get("minimum") if isinstance(salary, dict) else None
        currency = salary.get("currency") if isinstance(salary, dict) else ""
        comparable = bool(currency) and str(job.get("salary_currency", "")).casefold() == str(currency).casefold()
        salary_score = 10 if comparable or not minimum else 5
        if minimum and comparable:
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
    dimensions = apply_score_weights(dimensions, preferences)
    maximums = configured_score_weights(preferences)
    strategy = next((item for item in strategies if item.get("id") == eligibility.route), None)
    # A priority/watchlist company is a reason to look at its engineering roles, not at its
    # procurement or pre-sales openings, and the floor never touches the title match
    # itself because that dimension is the answer to "is this my job?". Watchlist gets a
    # smaller floor than priority - "lighter weight" is the field's own documented intent.
    if strategy and same_role_family:
        if strategy.get("kind") == "priority_company":
            _raise_dimensions_to_floor(dimensions, 55, maximums)
        elif strategy.get("kind") == "company_watchlist":
            _raise_dimensions_to_floor(dimensions, 45, maximums)
    if eligibility.status == EligibilityStatus.INELIGIBLE:
        _cap_dimensions(dimensions, INELIGIBLE_SCORE_CAP)
    total = sum(dimensions.values())
    verdict = compute_verdict(eligibility.status, total, eligibility.threshold)
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
    for requirement in extract_experience_requirements(str(job.get("description", "")), known_terms):
        if requirement["unmet"]:
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


def _raise_dimensions_to_floor(dimensions: dict[str, int], floor: int, maximums: dict[str, int]) -> None:
    shortfall = max(0, floor - sum(dimensions.values()))
    for key in ("domain_experience", "stack"):
        increase = min(shortfall, maximums[key] - dimensions[key])
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
