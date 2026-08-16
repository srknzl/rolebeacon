from __future__ import annotations

from rolebeacon.domain import EligibilityStatus
from rolebeacon.profile import CandidateProfileV1, MobilityProfileV1, SearchPreferencesV1, generate_strategies
from rolebeacon.scoring import evaluate_eligibility, extract_experience_requirements, rule_score

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
        "company_watchlist": ["Netflix"],
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


def test_a_short_country_code_does_not_match_a_substring_of_an_unrelated_place_name() -> None:
    # "de" inside "Île-de-France" would previously match country_code "DE" case-insensitively,
    # since hyphens count as regex word boundaries - a French posting must not pass as Germany.
    result = evaluate(
        job(
            location="Île-de-France, France",
            remote_scope="",
            description="Relocation support and visa sponsorship are available.",
        )
    )

    assert result.route != "relocate-de"
    assert result.status != EligibilityStatus.ELIGIBLE


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


def test_company_watchlist_strategy_has_a_weaker_score_floor_than_priority() -> None:
    # company_watchlist is documented as "lighter weight" than priority_companies, so it must get
    # a real but visibly smaller floor - not zero, and not as strong as priority's. A generic
    # engineering title (not a full target-role match) keeps the pre-floor score below either
    # floor, so raising it to 45 rather than 55 is actually observable.
    target = job(
        title="Site Reliability Engineer", company="Netflix", location="Unknown", remote_scope="",
        description="General software engineering role",
    )
    eligibility = evaluate(target)
    score = rule_score(
        target,
        eligibility,
        PREFERENCES.model_dump(mode="json"),
        CANDIDATE.model_dump(mode="json"),
        STRATEGIES,
    )

    assert eligibility.route == "company-watchlist"
    assert score.total == 45


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
    assert score(job(title="Site Reliability Engineer")).dimensions["role_domain"] == 17
    assert score(job(title="Staff Distributed Systems Engineer")).dimensions["role_domain"] == 30


def test_a_priority_company_does_not_lift_a_role_the_candidate_is_not_looking_for() -> None:
    # The floor exists to surface a wanted company's engineering roles, not its procurement openings.
    result = score(job(company="Google", title="Manager, Procurement", location="Unknown", remote_scope=""))

    assert result.dimensions["role_domain"] == 2


def test_extract_experience_requirements_parses_years_and_skill() -> None:
    found = extract_experience_requirements(
        "We need 5 years of Java experience and 3+ years of experience with Kafka. "
        "General software background is a plus."
    )

    assert {"skill": "Java", "years": 5} in found
    assert {"skill": "Kafka", "years": 3} in found


def test_extract_experience_requirements_keeps_the_longest_years_per_skill() -> None:
    found = extract_experience_requirements("2 years of Go required, though 5 years of Go is preferred.")

    assert found == [{"skill": "Go", "years": 5}]


def test_extract_experience_requirements_finds_nothing_in_plain_text() -> None:
    assert extract_experience_requirements("Build great products with a small team.") == []


def test_extract_experience_requirements_does_not_capture_of_as_a_skill() -> None:
    # Real posting text (Google Public Sector AI/ML role): "years of experience <verb>ing in ..."
    # doesn't match the "experience with/in/using X" shape, so "of" must not fall through as the
    # skill itself - a missed requirement is fine here, a bogus "of" gap is not.
    found = extract_experience_requirements("8 years of experience programming in C++, Java, Python, Kotlin or Go.")

    assert all(item["skill"].casefold() != "of" for item in found)


def test_extract_experience_requirements_ignores_an_unrelated_years_mention() -> None:
    # Real posting text (stock-plan clause): no "of"/"experience with|in|using" connector at all,
    # so the bare "10 years to exercise" must not be mistaken for an experience requirement.
    found = extract_experience_requirements(
        "You can participate in secondary offerings and have 10 years to exercise your options."
    )

    assert found == []


def test_extract_experience_requirements_does_not_capture_generic_filler_as_a_skill() -> None:
    # Real posting fragments where a generic qualifier sits where a skill name would go - none of
    # these are a skill, so a miss is correct and a bogus filler word is not.
    cases = [
        "5+ years of non-internship front-end engineering experience.",  # -> "non-internship"
        "8+ years of software development experience with a primary focus on backend systems.",
        "3+ years of full software development life cycle, including coding.",
        "8+ years of experience in one or more of the following areas: machine learning.",
        "Minimum 5 years of hands-on experience with enterprise backup and recovery.",
        "1 year of experience with state of the art GenAI techniques.",
    ]
    for description in cases:
        found = extract_experience_requirements(description)
        assert all(item["skill"].casefold() not in {"non-internship", "software", "full software", "one", "hands-on", "state"} for item in found), description


def test_extract_experience_requirements_does_not_capture_a_verb_before_the_real_object() -> None:
    # Real posting fragments: the word right after the connector is a verb describing the
    # activity ("leading", "developing", "building", "architecting"...), not the skill itself.
    cases = [
        "5+ years of leading design or architecture.",
        "3 years of experience with developing large-scale infrastructure.",
        "1+ years of providing technical leadership and project management.",
        "5 years of experience in architecting and designing complex cloud infrastructure.",
    ]
    for description in cases:
        found = extract_experience_requirements(description)
        assert all(not item["skill"].casefold().split()[0].endswith("ing") for item in found), description


def test_rule_score_flags_an_experience_requirement_the_candidate_profile_does_not_show() -> None:
    result = score(job(description="Build distributed systems. Requires 6 years of Rust experience."))

    assert any("6+ years of Rust" in gap["requirement"] for gap in result.gaps)


def test_rule_score_does_not_flag_an_experience_requirement_the_candidate_already_has() -> None:
    result = score(job(description="Build distributed systems. Requires 4 years of Java experience."))

    assert not any("years of Java" in gap["requirement"] for gap in result.gaps)
