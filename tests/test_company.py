from __future__ import annotations

from datetime import UTC, datetime

from rolebeacon.company import CompanyResearchService
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob, EligibilityResult, EligibilityStatus, ScoreResult
from rolebeacon.llm import LlmClient


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
