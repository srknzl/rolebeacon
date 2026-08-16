from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from rolebeacon.company import CompanyResearchService
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob, EligibilityResult, EligibilityStatus, ScoreResult
from rolebeacon.llm import LlmClient
from rolebeacon.setup import SetupService


def test_deterministic_company_fit_uses_only_evidence(tmp_path) -> None:
    settings = Settings.load()
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    service = CompanyResearchService(settings, database, LlmClient(settings))
    evidence = [
        {
            "source_url": "https://example.com/careers",
            "source_type": "careers",
            "title": "Careers",
            "excerpt": "Our backend platform team builds distributed systems. Relocation support is available.",
        }
    ]
    search_profile = {
        "preferred_domains": ["distributed systems", "backend"],
        "priority_companies": [],
        "company_watchlist": [],
    }

    profile, score = service._deterministic_research("Example", evidence, [], search_profile)

    assert profile["relocation"] == "available"
    assert score["dimensions"]["domain_alignment"] > 8
    assert score["total"] == sum(score["dimensions"].values())
    assert score["reasons"][0]["source_url"] == "https://example.com/careers"


def test_company_summary_states_extracted_facts_rather_than_quoting_employer_marketing() -> None:
    settings = Settings.load()
    service = CompanyResearchService(settings, Database(Path(":memory:")), LlmClient(settings))
    evidence = [{
        "source_url": "https://canonical.com/careers",
        "source_type": "careers",
        "title": "Careers",
        "excerpt": (
            "Canonical is the publisher of Ubuntu, the leading operating system for cloud computing. "
            "We are a remote-first company and hire from anywhere in the world. "
            "We offer a relocation package for roles based in an office."
        ),
    }]

    profile, _ = service._deterministic_research(
        "Canonical", evidence, [], {"preferred_domains": [], "priority_companies": [], "company_watchlist": []}
    )

    assert profile["summary"] == (
        "Remote work is described as worldwide. Visa sponsorship is not stated in the fetched sources. "
        "Relocation support is stated as available."
    )
    assert "publisher of Ubuntu" not in profile["summary"]


def test_a_reason_is_dropped_rather_than_citing_a_source_that_does_not_support_it() -> None:
    settings = Settings.load()
    service = CompanyResearchService(settings, Database(Path(":memory:")), LlmClient(settings))
    evidence = [{
        "source_url": "https://example.com/careers",
        "source_type": "careers",
        "title": "Careers",
        "excerpt": "We hire engineers in Berlin. Interviews are held on-site.",
    }]

    profile, score = service._deterministic_research(
        "Example", evidence, [], {"preferred_domains": ["payments"], "priority_companies": [], "company_watchlist": []}
    )

    assert all(item["source_url"] for item in score["reasons"])
    assert not any("payments" in item["claim"] for item in score["reasons"])
    assert any(item["source_url"] == "" for item in profile["risks"])


def test_a_fact_is_matched_inside_one_sentence_so_the_claim_can_quote_it() -> None:
    settings = Settings.load()
    service = CompanyResearchService(settings, Database(Path(":memory:")), LlmClient(settings))
    evidence = [{
        "source_url": "https://example.com/careers",
        "source_type": "careers",
        "title": "Careers",
        "excerpt": (
            "We build payments infrastructure for banks. "
            "Our teams work remotely within the European Union. "
            "This role does not offer visa sponsorship."
        ),
    }]

    profile, score = service._deterministic_research(
        "Example", evidence, [], {"preferred_domains": ["payments"], "priority_companies": [], "company_watchlist": []}
    )

    domain_reason = next(item for item in score["reasons"] if "payments" in item["claim"])
    assert domain_reason["quote"] == "We build payments infrastructure for banks."
    assert profile["sponsorship"] == "unavailable"
    sponsorship_risk = next(item for item in profile["risks"] if "sponsorship is unavailable" in item["claim"])
    assert sponsorship_risk["quote"] == "This role does not offer visa sponsorship."


def test_one_job_posting_cannot_establish_worldwide_remote_work_on_its_own() -> None:
    settings = Settings.load()
    service = CompanyResearchService(settings, Database(Path(":memory:")), LlmClient(settings))
    posting = {
        "source_url": "https://board.example/jobs/1",
        "source_type": "current_job_posting",
        "title": "Backend Engineer",
        "excerpt": "Backend Engineer. You can work from anywhere in the world.",
    }
    search_profile = {"preferred_domains": [], "priority_companies": [], "company_watchlist": []}

    alone, _ = service._deterministic_research("Example", [posting], [], search_profile)
    assert alone["remote_policy"] == "unknown"

    agreeing = {**posting, "source_url": "https://board.example/jobs/2"}
    corroborated, _ = service._deterministic_research("Example", [posting, agreeing], [], search_profile)
    assert corroborated["remote_policy"] == "worldwide"

    official = {**posting, "source_url": "https://example.com/careers", "source_type": "careers"}
    stated, _ = service._deterministic_research("Example", [official], [], search_profile)
    assert stated["remote_policy"] == "worldwide"


def test_brand_pages_are_excluded_from_every_hiring_signal() -> None:
    settings = Settings.load()
    service = CompanyResearchService(settings, Database(Path(":memory:")), LlmClient(settings))
    evidence = [{
        "source_url": "https://example.com/about",
        "source_type": "about",
        "title": "About",
        "excerpt": "We are a distributed cloud platform company offering relocation to a better future.",
    }]

    profile, score = service._deterministic_research(
        "Example", evidence, [], {"preferred_domains": ["cloud"], "priority_companies": [], "company_watchlist": []}
    )

    assert profile["relocation"] == "unknown"
    assert score["reasons"] == []


async def test_a_refresh_revalidates_a_stored_page_instead_of_downloading_it_again(tmp_path, monkeypatch) -> None:
    settings = Settings.load(tmp_path)
    service = CompanyResearchService(settings, Database(tmp_path / "jobs.sqlite3"), LlmClient(settings))
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(dict(request.headers))
        return httpx.Response(304)

    monkeypatch.setattr(
        "rolebeacon.company.default_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    cache = {
        "https://example.com/careers": {
            "source_type": "careers", "title": "Careers", "excerpt": "We hire engineers remotely.",
            "etag": 'W/"abc"', "last_modified": "Wed, 21 Oct 2026 07:28:00 GMT",
        }
    }

    evidence = await service._fetch_official_sources(
        [{"url": "https://example.com/careers", "type": "careers"}], cache
    )

    assert seen[0]["if-none-match"] == 'W/"abc"'
    assert seen[0]["if-modified-since"] == "Wed, 21 Oct 2026 07:28:00 GMT"
    assert evidence == [{
        "source_url": "https://example.com/careers", "source_type": "careers", "title": "Careers",
        "excerpt": "We hire engineers remotely.", "etag": 'W/"abc"',
        "last_modified": "Wed, 21 Oct 2026 07:28:00 GMT",
    }]


def test_a_page_that_answers_200_with_not_found_is_not_a_source() -> None:
    assert CompanyResearchService._soft_404("Page not found · Example", "Try the home page instead.")
    assert CompanyResearchService._soft_404("Example", "The page you requested no longer exists.")
    assert not CompanyResearchService._soft_404("Careers", "We hire engineers across our platform teams.")


def test_conventional_official_sources_skip_brand_pages_for_hiring_pages() -> None:
    urls = [item["url"] for item in CompanyResearchService._conventional_official_sources("example.com")]

    assert not any(url.endswith(("/about", "/company")) for url in urls)
    assert "https://example.com/careers" in urls


def test_redirected_product_page_is_reclassified_as_non_hiring_evidence() -> None:
    result = CompanyResearchService._validated_source_type(
        "careers",
        "https://example.com/products/job-search",
        "Job Search - publish your job postings",
        "Help candidates discover jobs published by your business.",
    )

    assert result == "official"


def test_coverage_counts_established_facts_rather_than_fetched_pages() -> None:
    jobs = [{"salary_min": 90000, "salary_max": 120000}]
    nothing_established = {
        "remote_policy": "unknown", "sponsorship": "unknown", "relocation": "unknown",
        "engineering_signals": [],
    }

    assert CompanyResearchService._fact_coverage(nothing_established, []) == (0.2, 0)
    # Five pages fetched but no fact stated must not score above one page that states three.
    assert CompanyResearchService._fact_coverage(
        {
            "remote_policy": "regional", "sponsorship": "unavailable", "relocation": "unknown",
            "engineering_signals": [{"claim": "Kafka", "source_url": "https://example.com/careers"}],
        },
        jobs,
    ) == (0.76, 8)


def test_company_evidence_is_deduplicated_across_mirrored_postings(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    service = CompanyResearchService(settings, database, LlmClient(settings))
    duplicate_job = {
        "source_url": "https://board.example/jobs/1",
        "source_type": "current_job_posting",
        "title": "Backend Engineer",
        "excerpt": "Backend Engineer Build distributed systems.",
    }
    evidence = service._deduplicate_evidence([
        duplicate_job,
        {**duplicate_job, "source_url": "https://mirror.example/jobs/1"},
        {
            "source_url": "https://example.com/careers",
            "source_type": "careers",
            "title": "Careers",
            "excerpt": "Our careers page describes remote engineering roles.",
        },
        {
            "source_url": "https://example.com/jobs",
            "source_type": "careers",
            "title": "Jobs",
            "excerpt": "Current openings across our engineering organization.",
        },
    ])

    assert len(evidence) == 3
    assert [item["source_url"] for item in evidence].count("https://board.example/jobs/1") == 1


async def test_no_key_registry_failure_does_not_break_company_research(tmp_path, monkeypatch) -> None:
    settings = Settings.load(tmp_path)
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    database.upsert_job(
        CollectedJob(
            source="fixture", source_job_id="registry-offline", title="Backend Engineer", company="Example",
            location="Remote", description="Build distributed services", url="https://jobs.example.test/1",
            published_at=datetime.now(UTC),
        )
    )
    service = CompanyResearchService(settings, database, LlmClient(settings))

    async def unavailable_registry(_name: str):
        return "", []

    monkeypatch.setattr(service, "_wikidata_entry", unavailable_registry)
    company_id = await service.research("Example")

    company = database.get_company(company_id)
    assert company is not None
    assert company["coverage_label"] == "low"
    assert company["evidence_count"] == 1


def test_company_profile_hides_catalog_fields_and_explains_coverage(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from rolebeacon.app import create_app

    settings = SetupService(Settings.load(tmp_path)).complete({
        "candidate": {
            "schema_version": "1.0", "name": "Candidate",
            "location": {"country_code": "TR", "country_name": "Türkiye"}, "skills": {},
        },
        "mobility": {"schema_version": "1.0", "current_country_code": "TR", "work_authorizations": ["TR"]},
        "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
        "enabled_source_ids": [],
        "llm": {"mode": "rules", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"},
        "activate": False,
    })
    app = create_app(settings)
    company_id = app.state.database.save_company_research(
        name="Example",
        domain="example.com",
        profile={
            "summary": "Evidence-backed employer assessment.", "remote_policy": "unknown",
            "sponsorship": "unknown", "relocation": "unknown", "confidence": 0.75,
        },
        evidence=[{
            "source_url": "https://example.com/careers", "source_type": "careers",
            "title": "Careers", "excerpt": "Engineering careers",
        }],
        score={
            "total": 40,
            "dimensions": {
                "domain_alignment": 8, "engineering_environment": 6, "location_mobility": 4,
                "compensation": 5, "company_quality": 9, "evidence_confidence": 8,
            },
            "reasons": [], "risks": [],
        },
        provider="rules", model="test",
    )
    with TestClient(app) as client:
        response = client.get(f"/companies/{company_id}")

    assert response.status_code == 200
    assert "fact coverage 8/10" in response.text
    assert "1 unique sources · 1 official page types" in response.text
    assert "Industry unknown" not in response.text
    assert "Headquarters unknown" not in response.text
    assert "% confidence" not in response.text


def test_country_scoped_remote_policy_is_not_worldwide(tmp_path) -> None:
    settings = Settings.load()
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    service = CompanyResearchService(settings, database, LlmClient(settings))
    evidence = [
        {
            "source_url": "https://example.com/careers",
            "source_type": "careers",
            "title": "Careers",
            "excerpt": "Distributed roles let employees work from anywhere in your country of employment.",
        }
    ]

    profile, score = service._deterministic_research(
        "Example",
        evidence,
        [],
        {"preferred_domains": [], "priority_companies": [], "company_watchlist": []},
    )

    assert profile["remote_policy"] == "regional"
    assert score["dimensions"]["location_mobility"] == 8
    assert any("candidate's configured country" in item["claim"] for item in score["risks"])


def test_explicit_worldwide_remote_policy_is_recognized(tmp_path) -> None:
    settings = Settings.load()
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    service = CompanyResearchService(settings, database, LlmClient(settings))
    evidence = [
        {
            "source_url": "https://example.com/handbook",
            "source_type": "handbook",
            "title": "Remote handbook",
            "excerpt": "Our employees can work from any country.",
        }
    ]

    profile, score = service._deterministic_research(
        "Example",
        evidence,
        [],
        {"preferred_domains": [], "priority_companies": [], "company_watchlist": []},
    )

    assert profile["remote_policy"] == "worldwide"
    assert score["dimensions"]["location_mobility"] == 20


def test_role_specific_no_sponsorship_is_not_company_policy(tmp_path) -> None:
    settings = Settings.load()
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    service = CompanyResearchService(settings, database, LlmClient(settings))
    evidence = [
        {
            "source_url": "https://example.com/jobs/1",
            "source_type": "current_job_posting",
            "title": "Engineer",
            "excerpt": "This role does not offer visa sponsorship.",
        }
    ]

    profile, _ = service._deterministic_research(
        "Example",
        evidence,
        [],
        {"preferred_domains": [], "priority_companies": [], "company_watchlist": []},
    )

    assert profile["sponsorship"] == "unknown"


def test_opportunity_score_combines_job_and_company_fit(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    job_id, _ = database.upsert_job(
        CollectedJob(
            source="fixture",
            source_job_id="1",
            title="Backend Engineer",
            company="Example GmbH",
            location="Berlin, Germany",
            description="Java backend role with relocation support",
            url="https://example.com/jobs/1",
            published_at=datetime.now(UTC),
        )
    )
    database.save_evaluation(
        job_id,
        EligibilityResult(
            status=EligibilityStatus.ELIGIBLE,
            route="relocate-de",
            sponsorship="unknown",
            relocation="available",
            location_fit="germany_relocation",
            reasons=["Relocation is available"],
            risks=[],
        ),
        ScoreResult(
            total=80,
            dimensions={"role_domain": 20, "stack": 15, "domain_experience": 16, "seniority": 9, "location_authorization": 15, "salary_employment": 5},
            confidence=0.8,
            verdict="review",
            evidence=[],
            gaps=[],
            provider="rules",
            model="test",
        ),
        "scored",
    )
    database.save_company_research(
        name="Example",
        domain="example.com",
        profile={"summary": "Example", "confidence": 0.8},
        evidence=[{"source_url": "https://example.com/about", "source_type": "about", "title": "About", "excerpt": "Example company"}],
        score={
            "total": 60,
            "dimensions": {"domain_alignment": 15, "engineering_environment": 10, "location_mobility": 10, "compensation": 8, "company_quality": 7, "evidence_confidence": 10},
            "reasons": [],
            "risks": [],
        },
        provider="rules",
        model="test",
    )

    job = database.get_job(job_id)

    assert job["company_score"] == 60
    assert job["opportunity_score"] == 76
    # SQLite ROUND() returns a REAL, which the templates would render as "76.0".
    assert isinstance(job["opportunity_score"], int)


async def test_configured_llm_company_research_never_silently_falls_back_to_rules(tmp_path, monkeypatch) -> None:
    payload = {
        "candidate": {"schema_version": "1.0", "name": "Candidate", "location": {"country_code": "TR", "country_name": "Türkiye"}, "skills": {}},
        "mobility": {"schema_version": "1.0", "current_country_code": "TR", "work_authorizations": ["TR"]},
        "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
        "enabled_source_ids": [],
        "llm": {"mode": "custom", "base_url": "http://model.example/v1", "model": "test-model"},
        "activate": True,
    }
    settings = SetupService(Settings.load(tmp_path)).complete(payload)
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_job(
        CollectedJob(
            source="fixture", source_job_id="llm-company", title="Backend Engineer", company="Example",
            location="Remote", description="Build systems", url="https://example.test/jobs/1", published_at=datetime.now(UTC),
        )
    )
    service = CompanyResearchService(settings, database, LlmClient(settings))

    async def available() -> bool:
        return True

    async def invalid_response(*_args, **_kwargs):
        return {
            "summary": "Example", "industry": "", "headquarters": "", "size": "", "remote_policy": "unknown",
            "sponsorship": "unknown", "relocation": "unknown", "engineering_signals": [], "risks": [], "confidence": 0.5,
            "score": {"total": 99, "dimensions": {"domain_alignment": 1, "engineering_environment": 1, "location_mobility": 1, "compensation": 1, "company_quality": 1, "evidence_confidence": 1}, "reasons": [], "risks": []},
        }

    monkeypatch.setattr(service.llm, "available", available)
    monkeypatch.setattr(service, "_llm_research", invalid_response)

    from rolebeacon.llm import LlmUnavailable

    with pytest.raises(LlmUnavailable, match="invalid company assessment"):
        await service.research("Example")
    assert database.list_companies() == []


def test_registrable_host_uses_public_suffix_rules() -> None:
    assert CompanyResearchService._registrable_host("careers.example.co.uk") == "example.co.uk"
    assert CompanyResearchService._registrable_host("jobs.example.com.tr") == "example.com.tr"


def test_operational_within_region_wording_does_not_establish_remote_policy(tmp_path) -> None:
    service = CompanyResearchService(Settings.load(), Database(tmp_path / "jobs.sqlite3"), LlmClient(Settings.load()))
    evidence = [{
        "source_url": "https://example.test/jobs/1", "source_type": "current_job_posting",
        "title": "Program Manager", "excerpt": "Own project implementation within the region and coordinate delivery.",
    }]

    profile, _ = service._deterministic_research(
        "Example", evidence, [], {"preferred_domains": [], "priority_companies": [], "company_watchlist": []},
    )

    assert profile["remote_policy"] == "unknown"


def test_short_engineering_skills_use_token_boundaries_and_keep_per_signal_citations(tmp_path) -> None:
    service = CompanyResearchService(Settings.load(), Database(tmp_path / "jobs.sqlite3"), LlmClient(Settings.load()))
    evidence = [
        {"source_url": "https://example.test/jobs/1", "source_type": "current_job_posting", "title": "Google role", "excerpt": "Google hires engineers."},
        {"source_url": "https://example.test/jobs/2", "source_type": "current_job_posting", "title": "Backend", "excerpt": "Build services in Go."},
    ]
    profile, _ = service._deterministic_research(
        "Example", evidence, [], {"preferred_skills": ["Go"], "preferred_domains": [], "priority_companies": [], "company_watchlist": []},
    )

    assert profile["engineering_signals"] == [{
        "claim": "Engineering signal: Go", "source_url": "https://example.test/jobs/2", "quote": "Build services in Go.",
    }]


def test_company_model_citations_must_belong_to_fetched_evidence() -> None:
    result = {
        "summary": "Example", "remote_policy": "unknown", "sponsorship": "unknown", "relocation": "unknown",
        "engineering_signals": [{"claim": "Go", "source_url": "https://fabricated.test"}], "risks": [],
        "confidence": .5,
        "score": {"total": 0, "dimensions": {"domain_alignment": 0, "engineering_environment": 0, "location_mobility": 0, "compensation": 0, "company_quality": 0, "evidence_confidence": 0}, "reasons": [], "risks": []},
    }

    with pytest.raises(ValueError, match="was not fetched"):
        CompanyResearchService._validate_company_result(result, [{"source_url": "https://example.test"}])
