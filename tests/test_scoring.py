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


def test_europe_relocation_target_matches_european_roles_but_requires_sponsorship() -> None:
    mobility = MobilityProfileV1.model_validate(
        {
            **MOBILITY.model_dump(mode="json"),
            "relocation_targets": [{"country_code": "EUROPE", "country_name": "Europe", "cities": []}],
        }
    )
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, mobility, PREFERENCES)]
    eligible = evaluate_eligibility(
        job(location="Berlin, Germany", description="Visa sponsorship available. Build backend systems."),
        PREFERENCES.model_dump(mode="json"), mobility.model_dump(mode="json"), strategies,
    )
    rejected = evaluate_eligibility(
        job(location="Paris, France", description="No visa sponsorship. Build backend systems."),
        PREFERENCES.model_dump(mode="json"), mobility.model_dump(mode="json"), strategies,
    )

    assert eligible.status == EligibilityStatus.ELIGIBLE
    assert eligible.route == "relocate-europe"
    assert rejected.status == EligibilityStatus.INELIGIBLE
    assert rejected.route == "relocate-europe"


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


def score(value):
    return rule_score(
        value,
        evaluate(value),
        PREFERENCES.model_dump(mode="json"),
        CANDIDATE.model_dump(mode="json"),
        STRATEGIES,
    )


def test_a_different_job_family_scores_near_zero_on_title_match() -> None:
    # Each of these contains an engineering word while being a different job.
    for title in ("Engineering Manager, Platform", "Senior Product Manager", "Customer Engineer (Pre-Sales)"):
        result = score(job(title=title))

        assert result.dimensions["role_domain"] == 2, title
        assert any("different role" in gap["requirement"] for gap in result.gaps), title


def test_any_engineering_title_matches_and_a_target_role_matches_fully() -> None:
    assert score(job(title="Site Reliability Engineer")).dimensions["role_domain"] == 14
    assert score(job(title="Staff Distributed Systems Engineer")).dimensions["role_domain"] == 25


def test_a_priority_company_does_not_lift_a_role_the_candidate_is_not_looking_for() -> None:
    # The floor exists to surface a wanted company's engineering roles, not its procurement openings.
    result = score(job(company="Google", title="Manager, Procurement", location="Unknown", remote_scope=""))

    assert result.dimensions["role_domain"] == 2
    assert result.total < 55
