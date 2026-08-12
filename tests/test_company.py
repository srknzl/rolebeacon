from __future__ import annotations

from datetime import UTC, datetime

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


def test_company_summary_uses_complete_sentences_instead_of_character_truncation(tmp_path) -> None:
    settings = Settings.load()
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    service = CompanyResearchService(settings, database, LlmClient(settings))
    evidence = [{
        "source_url": "https://example.com/about",
        "source_type": "about",
        "title": "About",
        "excerpt": "First complete sentence. " + "Second sentence with useful detail. " * 50,
    }]

    profile, _ = service._deterministic_research(
        "Example", evidence, [], {"preferred_domains": [], "priority_companies": [], "company_watchlist": []}
    )

    assert profile["summary"].endswith("detail.")
    assert not profile["summary"].endswith("…")


def test_company_evidence_is_deduplicated_and_coverage_uses_distinct_official_types(tmp_path) -> None:
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
    assert service._evidence_coverage(evidence) == (0.75, "moderate", 8)
    evidence.append({
        "source_url": "https://example.com/engineering",
        "source_type": "engineering",
        "title": "Engineering",
        "excerpt": "How our engineering teams build and operate services.",
    })
    assert service._evidence_coverage(evidence) == (0.9, "strong", 10)


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
    assert "source quality 8/10" in response.text
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
