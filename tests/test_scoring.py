from __future__ import annotations

import pytest

from rolebeacon.domain import EligibilityStatus, ScoreResult
from rolebeacon.profile import CandidateProfileV1, MobilityProfileV1, SearchPreferencesV1, generate_strategies
from rolebeacon.scoring import (
    _title_seniority,
    candidate_terms,
    clearance_requirements,
    evaluate_eligibility,
    extract_experience_requirements,
    location_requirement,
    rule_score,
    scoring_behavior_version,
    term_present,
)

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


def test_north_america_relocation_target_matches_a_us_state_and_city_location() -> None:
    # Regression guard for the region-match rewrite: NORTH_AMERICA used to be dead (only
    # "EUROPE" was special-cased), so "US, WA, Seattle" fell through to route=other/unknown.
    mobility = MobilityProfileV1.model_validate(
        {
            **MOBILITY.model_dump(mode="json"),
            "relocation_targets": [{"country_code": "NORTH_AMERICA", "country_name": "North America", "cities": []}],
        }
    )
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, mobility, PREFERENCES)]
    result = evaluate_eligibility(
        job(location="US, WA, Seattle", remote_scope="", description="Build backend systems."),
        PREFERENCES.model_dump(mode="json"), mobility.model_dump(mode="json"), strategies,
    )

    assert result.location_fit == "sponsorship-unknown:NORTH_AMERICA"


def test_remote_job_scoped_to_a_non_home_country_is_flagged_remote_scoped() -> None:
    # "Remote, United States" must not silently take on the candidate's own remote-from-tr
    # route/fit - it is scoped to a country the candidate hasn't confirmed sponsorship for.
    result = evaluate(job(location="Remote, United States", remote_scope="US only"))

    assert result.location_fit == "remote-scoped:US"
    assert result.route != "remote-from-tr"


def test_gb_alias_matches_a_constituent_nation_name() -> None:
    mobility = MobilityProfileV1.model_validate(
        {
            **MOBILITY.model_dump(mode="json"),
            "relocation_targets": [{"country_code": "GB", "country_name": "United Kingdom", "cities": []}],
        }
    )
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, mobility, PREFERENCES)]
    result = evaluate_eligibility(
        job(location="London, England", remote_scope="", description="Visa sponsorship available."),
        PREFERENCES.model_dump(mode="json"), mobility.model_dump(mode="json"), strategies,
    )

    assert result.route == "relocate-gb"
    assert result.status == EligibilityStatus.ELIGIBLE


def test_europe_relocation_target_matches_a_member_alias_not_just_its_full_name() -> None:
    # Regression guard: the region-member loop used to pass code="" purely to suppress bare
    # 2-letter-code false positives, which also silently dropped every member's own
    # COUNTRY_LOCATION_ALIASES - a job posted as "London, UK" or "Remote UK" (never spelling out
    # "United Kingdom") was invisible to a Europe relocation strategy for exactly that reason,
    # even though the identical single-country GB strategy already resolved it correctly.
    mobility = MobilityProfileV1.model_validate(
        {
            **MOBILITY.model_dump(mode="json"),
            "relocation_targets": [{"country_code": "EUROPE", "country_name": "Europe", "cities": []}],
        }
    )
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, mobility, PREFERENCES)]
    result = evaluate_eligibility(
        job(location="Remote UK", remote_scope="", description="Visa sponsorship available."),
        PREFERENCES.model_dump(mode="json"), mobility.model_dump(mode="json"), strategies,
    )

    assert result.location_fit == "sponsorship:EUROPE"
    assert result.status == EligibilityStatus.ELIGIBLE


def test_location_requirement_renders_a_plain_sentence_per_prefix() -> None:
    assert "already authorized" in location_requirement("authorized:DE")
    assert "scoped to United States" in location_requirement("remote-scoped:US")
    assert location_requirement("") == "Location requirement could not be determined from the posting."
    assert "visa sponsorship is evaluated separately" in location_requirement("relocation:DE")


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


def test_relocation_support_never_substitutes_for_required_sponsorship() -> None:
    result = evaluate(job(location="Berlin, Germany", remote_scope="", description="Relocation assistance is available."))

    assert result.status == EligibilityStatus.UNKNOWN
    assert result.sponsorship == "unknown"
    assert result.relocation == "available"
    assert result.location_fit == "sponsorship-unknown:DE"


def test_company_blocklist_uses_identity_equality_not_substrings() -> None:
    preferences = {**PREFERENCES.model_dump(mode="json"), "company_blocklist": ["Meta", "Go"]}

    metabase = evaluate_eligibility(job(company="Metabase"), preferences, MOBILITY.model_dump(mode="json"), STRATEGIES)
    google = evaluate_eligibility(job(company="Google"), preferences, MOBILITY.model_dump(mode="json"), STRATEGIES)
    meta = evaluate_eligibility(job(company="Meta Platforms, Inc."), preferences, MOBILITY.model_dump(mode="json"), STRATEGIES)

    assert metabase.status != EligibilityStatus.INELIGIBLE
    assert google.status != EligibilityStatus.INELIGIBLE
    assert meta.status == EligibilityStatus.INELIGIBLE


@pytest.mark.parametrize("phrase", ["medical clearance", "invoice clearance", "clearance sale"])
def test_non_security_clearance_wording_does_not_override_local_authorization(phrase: str) -> None:
    value = job(
        location="Istanbul, Türkiye",
        remote_scope="",
        description=f"New hires complete a {phrase} before onboarding.",
    )

    result = evaluate(value)

    assert result.status == EligibilityStatus.ELIGIBLE
    assert result.location_fit == "authorized:TR"
    assert any("work authorization" in reason for reason in result.reasons)


def test_country_aliases_match_turkey_and_uk_without_short_code_substrings() -> None:
    authorized = MobilityProfileV1.model_validate({
        **MOBILITY.model_dump(mode="json"), "work_authorizations": ["TR", "GB"],
        "relocation_targets": [{"country_code": "GB", "country_name": "United Kingdom"}],
    })
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, authorized, PREFERENCES)]

    turkey = evaluate_eligibility(job(location="Istanbul, Turkey", remote_scope=""), PREFERENCES.model_dump(mode="json"), authorized.model_dump(mode="json"), strategies)
    uk = evaluate_eligibility(job(location="Remote UK", remote_scope="UK"), PREFERENCES.model_dump(mode="json"), authorized.model_dump(mode="json"), strategies)

    assert turkey.status == EligibilityStatus.ELIGIBLE
    assert uk.status == EligibilityStatus.ELIGIBLE


def test_second_work_authorization_without_a_relocation_target_still_gets_a_strategy() -> None:
    # A dual citizen who is already authorized in DE has no reason to list it as a
    # relocation target, and used to end up with no DE strategy at all.
    authorized = MobilityProfileV1.model_validate({
        **MOBILITY.model_dump(mode="json"), "work_authorizations": ["TR", "DE"], "relocation_targets": [],
    })
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, authorized, PREFERENCES)]
    local_de = next(item for item in strategies if item["country_code"] == "DE")
    result = evaluate_eligibility(
        job(location="Berlin, Germany", remote_scope="", description="Build Java and Go backend systems."),
        PREFERENCES.model_dump(mode="json"), authorized.model_dump(mode="json"), strategies,
    )

    assert local_de["kind"] == "authorized_local"
    assert local_de["country_name"] == "Germany"
    assert local_de["cities"] == []
    assert result.status == EligibilityStatus.ELIGIBLE
    assert result.location_fit == "authorized:DE"


def test_the_remote_strategy_names_the_country_its_code_belongs_to() -> None:
    """The candidate profile and the mobility profile can disagree; the strategy cannot."""
    candidate_in_germany = CandidateProfileV1.model_validate({
        **CANDIDATE.model_dump(mode="json"),
        "location": {"country_code": "DE", "country_name": "Germany", "city": "Berlin"},
    })
    remote = MobilityProfileV1.model_validate({
        **MOBILITY.model_dump(mode="json"), "remote_from_current_country": True,
    })

    strategies = [item.model_dump(mode="json") for item in generate_strategies(candidate_in_germany, remote, PREFERENCES)]
    from_current = next(item for item in strategies if item["kind"] == "remote")

    # Mobility says TR, the candidate profile says Germany. A strategy that carried both would
    # match a German job and then record "remote:TR" against it.
    assert (from_current["country_code"], from_current["country_name"]) == ("TR", "Türkiye")
    assert from_current["label"] == "Remote from Türkiye"


def test_the_remote_strategy_keeps_the_profile_name_when_the_two_agree() -> None:
    remote = MobilityProfileV1.model_validate({
        **MOBILITY.model_dump(mode="json"), "remote_from_current_country": True,
    })

    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, remote, PREFERENCES)]
    from_current = next(item for item in strategies if item["kind"] == "remote")

    assert (from_current["country_code"], from_current["country_name"]) == ("TR", "Türkiye")


def test_authorized_country_listed_as_a_relocation_target_keeps_its_cities() -> None:
    authorized = MobilityProfileV1.model_validate({
        **MOBILITY.model_dump(mode="json"), "work_authorizations": ["TR", "DE"],
    })
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, authorized, PREFERENCES)]
    local = [item for item in strategies if item["kind"] == "authorized_local"]

    assert [item["country_code"] for item in local] == ["DE", "TR"]
    assert local[0]["cities"] == ["Berlin"]
    assert local[1]["cities"] == ["Istanbul"]


def test_clearance_negation_preference_unknown_and_explicit_conflict() -> None:
    no_clearance = evaluate(job(description="No security clearance is required. Build Java services."))
    preferred = evaluate(job(description="Security clearance is preferred but not required."))
    required = evaluate(job(description="An active US Secret clearance is required."))
    conflicting_mobility = {
        **MOBILITY.model_dump(mode="json"),
        "clearance_policy": {"status": "cannot_meet", "willing_to_undergo_vetting": False},
    }
    conflict = evaluate_eligibility(
        job(description="An active US Secret clearance is required."), PREFERENCES.model_dump(mode="json"),
        conflicting_mobility, STRATEGIES,
    )

    assert no_clearance.status == EligibilityStatus.ELIGIBLE
    assert preferred.status == EligibilityStatus.ELIGIBLE
    assert any("preferred, not required" in risk for risk in preferred.risks)
    assert required.status == EligibilityStatus.UNKNOWN
    assert "active US Secret clearance is required" in required.risks[0]
    assert conflict.status == EligibilityStatus.INELIGIBLE


def test_rules_only_vocabulary_includes_verified_detailed_profile_evidence() -> None:
    candidate = {
        **CANDIDATE.model_dump(mode="json"),
        "experience": [{"company": "Example", "title": "Engineer", "highlights": ["Built Rust services"]}],
        "projects": [{"name": "Queue", "technologies": ["NATS"]}],
    }
    result = rule_score(
        job(description="Requires 5 years of Rust experience and NATS."), evaluate(job()),
        {**PREFERENCES.model_dump(mode="json"), "preferred_skills": ["Rust", "NATS"]}, candidate, STRATEGIES,
    )

    assert result.dimensions["stack"] == 10
    assert not any("years of Rust" in gap["requirement"] for gap in result.gaps)


def test_clearance_classifier_distinguishes_requirement_kinds_and_preserves_evidence() -> None:
    cases = {
        "This role does not require security clearance.": "not_required",
        "A TS/SCI clearance is preferred but not required.": "preferred",
        "You must have the ability to obtain a security clearance.": "ability_to_obtain",
        "An active top-secret clearance is required.": "active_required",
        "We build tools used in security clearance workflows.": "ambiguous",
    }
    for evidence, expected in cases.items():
        result = clearance_requirements(evidence)
        assert result[0]["kind"] == expected
        assert result[0]["evidence"] == evidence
    assert clearance_requirements("Sicherheitsüberprüfung erforderlich.") == []


def test_matching_active_clearance_continues_to_other_deterministic_rules() -> None:
    mobility = {
        **MOBILITY.model_dump(mode="json"),
        "clearance_policy": {
            "status": "has_active_clearance", "willing_to_undergo_vetting": True,
            "credentials": [{"jurisdiction": "US", "level": "Secret", "status": "active"}],
        },
    }
    result = evaluate_eligibility(
        job(location="Remote Worldwide", description="An active US Secret clearance is required."),
        {**PREFERENCES.model_dump(mode="json"), "exclude_phrases": []}, mobility, STRATEGIES,
    )

    assert result.status == EligibilityStatus.ELIGIBLE
    assert any("matching active clearance" in reason.casefold() for reason in result.reasons)


def test_clearance_rules_are_repeatable_and_user_exclusions_keep_precedence() -> None:
    preferences = {**PREFERENCES.model_dump(mode="json"), "exclude_phrases": ["TS/SCI"]}
    value = job(description="An active TS/SCI clearance is required.")
    results = [evaluate_eligibility(value, preferences, MOBILITY.model_dump(mode="json"), STRATEGIES) for _ in range(3)]

    assert all(result.status == EligibilityStatus.INELIGIBLE for result in results)
    assert len({tuple(result.risks) for result in results}) == 1


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


def test_us_state_postal_abbreviation_does_not_collide_with_the_same_letters_country_code() -> None:
    # "CA" is both California's postal abbreviation and Canada's ISO code (also DE/Delaware-Germany,
    # IN/Indiana-India, GA/Georgia-Georgia, ...). An onsite US posting naming the state must not be
    # mistaken for a match against a same-lettered country's relocation strategy.
    mobility = MobilityProfileV1.model_validate(
        {
            **MOBILITY.model_dump(mode="json"),
            "relocation_targets": [{"country_code": "CA", "country_name": "Canada", "cities": []}],
        }
    )
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, mobility, PREFERENCES)]
    result = evaluate_eligibility(
        job(location="San Mateo, CA, United States", remote_scope="", description="Build backend systems."),
        PREFERENCES.model_dump(mode="json"), mobility.model_dump(mode="json"), strategies,
    )

    assert result.route != "relocate-ca"
    assert result.status != EligibilityStatus.ELIGIBLE


def test_marketing_copy_mentioning_the_world_does_not_grant_worldwide_remote_eligibility() -> None:
    # A company's own "we connect people anywhere in the world" mission statement describes its
    # product, not this job's remote policy - it must not satisfy the worldwide-remote branch.
    mobility = MobilityProfileV1.model_validate({**MOBILITY.model_dump(mode="json"), "remote_from_current_country": True})
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, mobility, PREFERENCES)]
    result = evaluate_eligibility(
        job(
            location="Tokyo, Japan",
            remote_scope="",
            description="Our vision is to reimagine the way people come together, from anywhere in the world.",
        ),
        PREFERENCES.model_dump(mode="json"), mobility.model_dump(mode="json"), strategies,
    )

    assert result.status != EligibilityStatus.ELIGIBLE
    assert result.location_fit != "worldwide"


def test_a_time_boxed_work_from_anywhere_perk_does_not_grant_worldwide_remote_eligibility() -> None:
    # "Work from anywhere in the world for 30 days per year" next to a hybrid office requirement is
    # a bounded travel perk, not the job's actual remote eligibility.
    mobility = MobilityProfileV1.model_validate({**MOBILITY.model_dump(mode="json"), "remote_from_current_country": True})
    strategies = [item.model_dump(mode="json") for item in generate_strategies(CANDIDATE, mobility, PREFERENCES)]
    result = evaluate_eligibility(
        job(
            location="Zurich, Switzerland",
            remote_scope="",
            description="Work from anywhere in the world for 30 days per year. Three days a week in the office.",
        ),
        PREFERENCES.model_dump(mode="json"), mobility.model_dump(mode="json"), strategies,
    )

    assert result.status != EligibilityStatus.ELIGIBLE
    assert result.location_fit != "worldwide"


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


def _role_match_score(title: str, target_roles: list[str]) -> int:
    preferences = {**PREFERENCES.model_dump(mode="json"), "target_roles": target_roles}
    value = job(title=title)
    eligibility = evaluate_eligibility(value, preferences, MOBILITY.model_dump(mode="json"), STRATEGIES)
    return rule_score(value, eligibility, preferences, CANDIDATE.model_dump(mode="json"), STRATEGIES).dimensions[
        "role_domain"
    ]


def test_a_leftover_word_from_an_already_engineering_target_role_does_not_match_an_unrelated_title() -> None:
    # "Developer Experience Engineer" already contains "developer" and "engineer" - the specifics
    # fallback exists so a target role with NO engineering word of its own (e.g. "Data Scientist")
    # can still be recognized, not so a role that already has one can lend its one leftover word
    # ("experience") as free-standing proof of family membership to an unrelated, non-engineering title.
    target_roles = ["Developer Experience Engineer"]

    # No engineering term and no fallback-eligible specific word left -> not the same family,
    # same low outcome as any other unrelated title (role_domain 6, same_role_family False).
    assert _role_match_score("VP Client Success & Experience UK", target_roles) == 6
    # The same leftover word still earns its scoring bonus once the title is genuinely in-family.
    assert _role_match_score("Staff Developer Experience Engineer", target_roles) == 30


def test_a_target_role_with_no_engineering_word_still_matches_via_its_specifics() -> None:
    assert _role_match_score("Data Scientist", ["Data Scientist"]) == 30
    assert _role_match_score("Warehouse Operations Lead", ["Data Scientist"]) == 6


def test_a_priority_company_does_not_lift_a_role_the_candidate_is_not_looking_for() -> None:
    # The floor exists to surface a wanted company's engineering roles, not its procurement openings.
    result = score(job(company="Google", title="Manager, Procurement", location="Unknown", remote_scope=""))

    assert result.dimensions["role_domain"] == 2


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # "intern" is first in SENIORITY_LEVELS, so an unguarded substring match shadowed the
        # real level for every internal-tooling and international title.
        ("senior software engineer, internal tools", "senior"),
        ("staff engineer international", "staff"),
        ("principal engineer, internal developer platform", "principal"),
        # Real intern postings still match, in each of their usual spellings.
        ("backend internship", "intern"),
        ("software engineer intern", "intern"),
        ("summer interns, platform", "intern"),
        # The pre-existing "mid" guard is unchanged.
        ("midwest platform engineer", "unspecified"),
        ("middleware engineer", "unspecified"),
        ("mid to senior engineer", "mid"),
    ],
)
def test_title_seniority_matches_whole_words_only(title: str, expected: str) -> None:
    assert _title_seniority(title) == expected


def test_an_internal_tools_title_scores_full_marks_on_a_preferred_seniority() -> None:
    preferences = {**PREFERENCES.model_dump(mode="json"), "preferred_seniority": ["senior", "staff"]}
    value = job(title="Senior Software Engineer, Internal Tools")
    eligibility = evaluate_eligibility(value, preferences, MOBILITY.model_dump(mode="json"), STRATEGIES)
    result = rule_score(value, eligibility, preferences, CANDIDATE.model_dump(mode="json"), STRATEGIES)

    assert result.dimensions["seniority"] == 15


def _skill_score(description: str, skills: list[str], domains: list[str]) -> ScoreResult:
    preferences = {**PREFERENCES.model_dump(mode="json"), "preferred_skills": skills, "preferred_domains": domains}
    candidate = {**CANDIDATE.model_dump(mode="json"), "skills": {"Languages": skills}}
    value = job(description=description)
    eligibility = evaluate_eligibility(value, preferences, MOBILITY.model_dump(mode="json"), STRATEGIES)
    return rule_score(value, eligibility, preferences, candidate, STRATEGIES)


def test_short_skills_and_domains_do_not_match_inside_unrelated_words() -> None:
    # "Go" used to match "good", "C" matched "communicator", "AI" matched "Email" - each one
    # awarding points and a fabricated evidence row claiming the posting mentioned the skill.
    result = _skill_score(
        "We need someone who is a good communicator. Email us. Recruiting for a great gig.",
        ["Go", "R", "C"],
        ["AI"],
    )

    assert result.dimensions["stack"] == 0
    assert result.dimensions["domain_experience"] == 0
    assert not [item for item in result.evidence if item["requirement"] in {"Relevant skills", "Preferred domain"}]


def test_skills_with_trailing_punctuation_still_match_when_genuinely_present() -> None:
    result = _skill_score(
        "Experience with Go and C, plus C++, C#, .NET and Node.js on our AI platform.",
        ["Go", "C++", "C#", ".NET", "Node.js"],
        ["AI"],
    )
    evidence = {item["requirement"]: item["profile_evidence"] for item in result.evidence}

    assert result.dimensions["stack"] == 20
    assert result.dimensions["domain_experience"] == 5
    assert evidence["Relevant skills"] == "Go, C++, C#, .NET, Node.js"
    assert evidence["Preferred domain"] == "AI"


def test_term_present_folds_the_term_and_matches_only_whole_terms() -> None:
    # The text arrives already casefolded; only the term still needs folding.
    assert term_present("Go", "we write go and rust here")
    assert not term_present("Go", "a good day to golang")
    assert not term_present("C", "a communicator")
    assert not term_present("", "anything")


def test_extract_experience_requirements_parses_years_and_skill() -> None:
    found = extract_experience_requirements(
        "We need 5 years of Java experience and 3+ years of experience with Kafka. "
        "General software background is a plus."
    )

    assert {"skill": "Java", "years": 5, "unmet": True} in found
    assert {"skill": "Kafka", "years": 3, "unmet": True} in found


def test_extract_experience_requirements_keeps_the_longest_years_per_skill() -> None:
    found = extract_experience_requirements("2 years of Go required, though 5 years of Go is preferred.")

    assert found == [{"skill": "Go", "years": 5, "unmet": True}]


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


def test_extract_experience_requirements_captures_the_object_after_a_connector_verb() -> None:
    # "experience building/developing/working with/designing/leading X" has no bare "of X" or
    # "with X" shape at all, so it used to miss entirely; the connector verb is consumed and X
    # (not the verb) is the captured skill.
    found = extract_experience_requirements("5+ years of experience building distributed systems.")

    assert found == [{"skill": "distributed systems", "years": 5, "unmet": True}]


def test_extract_experience_requirements_accepts_a_possessive_years_phrasing() -> None:
    found = extract_experience_requirements("5 years' experience with Python.")

    assert found == [{"skill": "Python", "years": 5, "unmet": True}]


def test_extract_experience_requirements_drops_single_word_filler_not_in_known_terms() -> None:
    # "related"/"modern"/"quota" sit where a skill would go but are never a skill themselves, and
    # unlike "data structures" or "Object Oriented" they are a single lowercase word with nothing
    # (multi-word, a symbol, capitalization, or the candidate's own vocabulary) to redeem them.
    for description in ("3+ years of related experience.", "3+ years of modern experience.", "5+ years of quota experience."):
        assert extract_experience_requirements(description) == [], description


def test_extract_experience_requirements_keeps_multiword_and_capitalized_skills() -> None:
    found = extract_experience_requirements("5+ years of experience with data structures and 3+ years of Kubernetes.")

    assert {"skill": "data structures", "years": 5, "unmet": True} in found
    assert {"skill": "Kubernetes", "years": 3, "unmet": True} in found


def test_extract_experience_requirements_marks_a_skill_in_known_terms_as_met() -> None:
    found = extract_experience_requirements("5 years of Python and 5 years of Rust required.", {"python"})

    assert {"skill": "Python", "years": 5, "unmet": False} in found
    assert {"skill": "Rust", "years": 5, "unmet": True} in found


def test_a_multi_word_requirement_is_met_by_evidence_outside_the_skills_dict() -> None:
    # candidate_terms() keeps a skills-dict entry whole but tokenizes every evidence section, so
    # "distributed systems" used to be unmet for a candidate whose experience is full of it.
    known = candidate_terms(
        {
            "summary": "Ten years building distributed systems in production.",
            "experience": [{"highlights": ["Designed distributed systems at scale."]}],
            "skills": {"Languages": ["Java", "Go"]},
        }
    )

    found = extract_experience_requirements("Requires 5+ years of distributed systems experience.", known)

    assert found == [{"skill": "distributed systems", "years": 5, "unmet": False}]


def test_a_requirement_is_met_across_a_singular_plural_wording_difference() -> None:
    known = candidate_terms({"skills": {"Core": ["Distributed system"]}})

    found = extract_experience_requirements("Requires 5+ years of distributed systems experience.", known)

    assert found == [{"skill": "distributed systems", "years": 5, "unmet": False}]


def test_a_requirement_the_profile_never_shows_stays_unmet() -> None:
    known = candidate_terms(
        {"summary": "Ten years building distributed systems.", "skills": {"Languages": ["Java", "Go"]}}
    )

    assert extract_experience_requirements("Requires 5+ years of Rust.", known) == [
        {"skill": "Rust", "years": 5, "unmet": True}
    ]
    # Every word of a phrase must be shown, not just one of them.
    assert extract_experience_requirements("Requires 5+ years of embedded systems.", known) == [
        {"skill": "embedded systems", "years": 5, "unmet": True}
    ]


def test_single_word_requirements_are_unchanged() -> None:
    known = candidate_terms({"skills": {"Languages": ["Java", "Go"]}})

    assert extract_experience_requirements("Requires 5+ years of Java.", known) == [
        {"skill": "Java", "years": 5, "unmet": False}
    ]
    assert extract_experience_requirements("Requires 5+ years of Kafka.", known) == [
        {"skill": "Kafka", "years": 5, "unmet": True}
    ]


def test_the_widened_met_test_does_not_widen_what_counts_as_a_skill_name() -> None:
    # _is_plausible_skill still consults known_terms itself, not the tokenized vocabulary: a
    # skills-dict phrase must not make each of its words a plausible skill name on its own.
    known = candidate_terms({"skills": {"Core": ["Data structures"]}})

    assert "data" not in known
    assert extract_experience_requirements("5+ years of data.", known) == []
    assert extract_experience_requirements("5+ years of data structures.", known) == [
        {"skill": "data structures", "years": 5, "unmet": False}
    ]


def test_rule_score_flags_an_experience_requirement_the_candidate_profile_does_not_show() -> None:
    result = score(job(description="Build distributed systems. Requires 6 years of Rust experience."))

    assert any("6+ years of Rust" in gap["requirement"] for gap in result.gaps)


def test_rule_score_does_not_flag_an_experience_requirement_the_candidate_already_has() -> None:
    result = score(job(description="Build distributed systems. Requires 4 years of Java experience."))

    assert not any("years of Java" in gap["requirement"] for gap in result.gaps)


def test_custom_score_distribution_is_validated_and_deterministic() -> None:
    custom = {
        **PREFERENCES.model_dump(mode="json"),
        "score_weights": {
            "role_domain": 40,
            "stack": 30,
            "domain_experience": 0,
            "seniority": 10,
            "location_authorization": 15,
            "salary_employment": 5,
        },
    }
    validated = SearchPreferencesV1.model_validate(custom).model_dump(mode="json")
    eligibility = evaluate_eligibility(job(), validated, MOBILITY.model_dump(mode="json"), STRATEGIES)

    first = rule_score(job(), eligibility, validated, CANDIDATE.model_dump(mode="json"), STRATEGIES)
    second = rule_score(job(), eligibility, validated, CANDIDATE.model_dump(mode="json"), STRATEGIES)

    assert first == second
    assert first.dimensions["domain_experience"] == 0
    assert first.dimensions["role_domain"] <= 40
    assert sum(first.dimensions.values()) == first.total


def test_weight_change_has_one_stable_scoring_version_suffix() -> None:
    default = PREFERENCES.model_dump(mode="json")
    custom = {**default, "score_weights": {**default["score_weights"], "role_domain": 31, "stack": 19}}

    assert scoring_behavior_version(default) == scoring_behavior_version(default)
    assert scoring_behavior_version(default) != scoring_behavior_version(custom)


def test_score_distribution_requires_all_dimensions() -> None:
    invalid = {**PREFERENCES.model_dump(mode="json"), "score_weights": {"role_domain": 100}}

    with pytest.raises(ValueError, match="exactly the supported"):
        SearchPreferencesV1.model_validate(invalid)
