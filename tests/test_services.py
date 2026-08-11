from __future__ import annotations

from rolebeacon.services import cover_letter_recommendation, detect_ats, validate_candidate_profile


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
