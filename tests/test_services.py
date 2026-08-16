from __future__ import annotations

from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob
from rolebeacon.llm import LlmClient
from rolebeacon.services import ArtifactService, cover_letter_recommendation, detect_ats, validate_candidate_profile
from rolebeacon.setup import SetupService


def test_profile_validator_finds_end_date_contradiction() -> None:
    profile = {
        "experience": [
            {"company": "Example", "end": "2024", "highlights": ["Delivered migration work from 2023–2025."]}
        ]
    }

    assert validate_candidate_profile(profile) == ["Example ends in 2024, but a highlight mentions 2025"]


def test_cover_letter_is_recommended_for_relocation() -> None:
    recommended, reason = cover_letter_recommendation(
        {"route": "relocate-de", "company": "Example", "title": "Backend Engineer", "description": "", "score": 75}
    )

    assert recommended is True
    assert "relocation" in reason


def test_optional_cover_letter_is_not_default() -> None:
    recommended, _ = cover_letter_recommendation(
        {"route": "priority-companies", "company": "Example", "title": "Software Engineer", "description": "", "score": 84}
    )

    assert recommended is False


def test_detect_ats() -> None:
    assert detect_ats("https://boards.greenhouse.io/example/jobs/1") == "greenhouse"
    assert detect_ats("https://jobs.ashbyhq.com/example/1") == "ashby"
    assert detect_ats("https://example.com/careers/1") == "generic"


async def test_cover_letter_prompt_strips_contact_pii_and_supplies_mobility_facts(tmp_path, monkeypatch) -> None:
    payload = {
        "candidate": {
            "schema_version": "1.0",
            "name": "Example Candidate",
            "contact": {"email": "candidate@example.com", "phone": "+1", "linkedin": "https://linkedin.com/in/example", "github": "https://github.com/example"},
            "location": {"country_code": "TR", "country_name": "Türkiye", "city": "Istanbul"},
            "skills": {"Languages": ["Python", "Go"]},
            "experience": [],
            "projects": [],
            "education": [],
            "languages": [],
        },
        "mobility": {"schema_version": "1.0", "current_country_code": "TR", "work_authorizations": ["TR"]},
        "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
        "enabled_source_ids": [],
        "llm": {"mode": "rules", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"},
        "activate": True,
    }
    settings = SetupService(Settings.load(tmp_path)).complete(payload)
    database = Database(settings.database_path)
    database.initialize()
    job_id, _ = database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="1", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/1",
        )
    )
    llm = LlmClient(settings)
    captured: dict[str, str] = {}

    async def fake_generate_text(system, prompt, schema, name, validate=None):
        captured["prompt"] = prompt
        return {"subject": "Application", "paragraphs": ["Body text."] * 3}

    monkeypatch.setattr(llm, "generate_text", fake_generate_text)
    artifacts = ArtifactService(settings, database, llm)

    await artifacts.generate_cover_letter(job_id)

    assert "candidate@example.com" not in captured["prompt"]
    assert "linkedin.com" not in captured["prompt"]
    assert "work_authorizations" in captured["prompt"]
