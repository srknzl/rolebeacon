from __future__ import annotations

from rolebeacon.domain import EligibilityStatus
from rolebeacon.profile import CandidateProfileV1, MobilityProfileV1, SearchPreferencesV1, generate_strategies
from rolebeacon.scoring import evaluate_eligibility, rule_score

CANDIDATE = CandidateProfileV1.model_validate(
    {
        "schema_version": "1.0",
        "name": "Example Candidate",
        "summary": "Backend and distributed-systems engineer",
        "location": {"country_code": "TR", "country_name": "Türkiye", "city": "Istanbul"},
        "skills": {"Languages": ["Java", "Go", "TypeScript", "Python"]},
    }
)
MOBILITY = MobilityProfileV1.model_validate(
    {
        "schema_version": "1.0",
        "current_country_code": "TR",
        "work_authorizations": ["TR"],
        "relocation_targets": [
            {"country_code": "DE", "country_name": "Germany", "cities": ["Berlin"]},
            {"country_code": "US", "country_name": "United States", "cities": []},
        ],
    }
)
PREFERENCES = SearchPreferencesV1.model_validate(
    {
        "schema_version": "1.0",
        "target_roles": ["Backend Engineer", "Distributed Systems Engineer"],
        "preferred_skills": ["Java", "Go", "TypeScript", "Python"],
        "preferred_domains": ["distributed systems", "backend", "cloud infrastructure"],
        "exclude_phrases": ["citizens only", "security clearance required"],
        "priority_companies": ["Google", "Microsoft"],
    }
)
STRATEGIES = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, MOBILITY, PREFERENCES)]


def job(**overrides):
    value = {
        "title": "Senior Backend Engineer",
        "company": "Example",
        "location": "Remote Worldwide",
        "remote_scope": "Worldwide",
        "description": "Build Java and Go distributed systems with Kafka and PostgreSQL.",
        "salary_min": 120000,
        "salary_max": 150000,
    }
    value.update(overrides)
    return value


def evaluate(value):
    return evaluate_eligibility(
        value,
        PREFERENCES.model_dump(mode="json"),
        MOBILITY.model_dump(mode="json"),
        STRATEGIES,
    )


def test_foreign_role_without_sponsorship_is_ineligible() -> None:
    result = evaluate(
        job(
            location="United States",
            remote_scope="US only",
            description="Must be authorized to work without sponsorship.",
        )
    )

    assert result.status == EligibilityStatus.INELIGIBLE
    assert result.route == "relocate-us"


def test_remote_emea_is_not_assumed_to_include_turkiye() -> None:
    result = evaluate(job(location="Remote EMEA", remote_scope="EMEA"))

    assert result.status == EligibilityStatus.UNKNOWN
    assert result.location_fit == "remote-scope-unknown"


def test_country_scoped_remote_is_not_assumed_worldwide() -> None:
    result = evaluate(
        job(
            location="Remote",
            remote_scope="Country of employment",
            description="Employees can work from anywhere in your country of employment.",
        )
    )

    assert result.status == EligibilityStatus.UNKNOWN
    assert result.location_fit == "remote-scope-unknown"


def test_relocation_role_is_eligible() -> None:
    result = evaluate(
        job(location="Berlin, Germany", description="Relocation support and visa sponsorship are available.")
    )

    assert result.route == "relocate-de"
    assert result.status == EligibilityStatus.ELIGIBLE


def test_priority_company_strategy_has_score_floor() -> None:
    target = job(company="Google", location="Unknown", remote_scope="", description="General software engineering role")
    eligibility = evaluate(target)
    score = rule_score(
        target,
        eligibility,
        PREFERENCES.model_dump(mode="json"),
        CANDIDATE.model_dump(mode="json"),
        STRATEGIES,
    )

    assert eligibility.route == "priority-companies"
    assert score.total >= 55
