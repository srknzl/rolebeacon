from __future__ import annotations

from fastapi.testclient import TestClient

from rolebeacon.app import create_app
from rolebeacon.config import Settings
from rolebeacon.setup import SetupService


def _draft(**overrides) -> dict:
    draft = {
        "candidate": {
            "schema_version": "1.0",
            "name": "Ada Lovelace",
            "location": {"country_code": "TR", "country_name": "Türkiye"},
        },
        "mobility": {
            "schema_version": "1.0",
            "current_country_code": "TR",
            "work_authorizations": ["TR"],
        },
        "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
        "enabled_source_ids": ["arbeitnow"],
        "llm": {"mode": "rules"},
        "activate": False,
    }
    return {**draft, **overrides}


def test_a_complete_draft_is_ready_with_no_missing_facts() -> None:
    result = SetupService(Settings.load()).review(_draft())

    assert result["ready"] is True
    assert result["missing"] == []
    assert [item["title"] for item in result["items"]] == [
        "Candidate",
        "Target roles",
        "Work authorization",
        "Relocation",
        "Sponsorship",
        "Security clearance",
        "Salary",
        "Sources",
        "Scoring",
    ]


def test_an_empty_draft_names_every_missing_critical_fact() -> None:
    result = SetupService(Settings.load()).review({})

    assert result["ready"] is False
    assert result["missing"] == [
        "Candidate: No name",
        "Target roles: No target role",
        "Work authorization: No country you can work in today",
        "Sources: No source selected",
    ]


def test_unknown_clearance_is_ambiguous_but_never_blocks_setup() -> None:
    result = SetupService(Settings.load()).review(_draft())

    assert result["ready"] is True
    assert any("Security clearance: Unknown" in entry for entry in result["ambiguous"])


def test_a_hard_salary_filter_without_a_minimum_is_reported_as_ambiguous() -> None:
    draft = _draft()
    draft["preferences"]["salary"] = {"minimum": None, "currency": "", "hard_filter": True}

    result = SetupService(Settings.load()).review(draft)

    assert "Salary: Hard filter enabled without a minimum, so it rejects nothing" in result["ambiguous"]


def test_a_minimum_without_a_currency_is_reported_as_ambiguous() -> None:
    draft = _draft()
    draft["preferences"]["salary"] = {"minimum": 90000, "currency": "", "hard_filter": False}

    result = SetupService(Settings.load()).review(draft)

    assert any("no currency" in entry for entry in result["ambiguous"])


def test_relocation_targets_are_ignored_when_relocation_is_switched_off() -> None:
    draft = _draft()
    draft["mobility"]["willing_to_relocate"] = False
    draft["mobility"]["relocation_targets"] = [{"country_code": "DE", "country_name": "Germany"}]

    result = SetupService(Settings.load()).review(draft)

    assert any("willing to relocate' is off" in entry for entry in result["ambiguous"])


def test_regions_and_countries_are_both_named_in_the_relocation_summary() -> None:
    draft = _draft()
    draft["mobility"]["relocation_targets"] = [
        {"country_code": "EUROPE", "country_name": "Europe"},
        {"country_code": "DE", "country_name": "Germany"},
    ]

    result = SetupService(Settings.load()).review(draft)

    relocation = next(item for item in result["items"] if item["title"] == "Relocation")
    assert relocation["detail"] == "Europe, Germany (DE)"


def test_the_web_review_endpoint_returns_the_service_result() -> None:
    settings = Settings.load()
    settings.ensure_directories()
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/setup/review", json=_draft())

    assert response.status_code == 200
    # Both wizards must see one definition of completeness, so the endpoint adds nothing of its own.
    assert response.json() == SetupService(settings).review(_draft())
