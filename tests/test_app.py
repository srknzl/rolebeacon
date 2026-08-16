from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from time import sleep

import pytest
from fastapi.testclient import TestClient

from rolebeacon.app import _source_filter_options, create_app
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob, EligibilityResult, EligibilityStatus, ScoreResult, SourceConfig
from rolebeacon.llm import SCORING_RUBRIC, LlmClient
from rolebeacon.setup import SetupService


def setup_payload() -> dict:
    return {
        "candidate": {
            "schema_version": "1.0",
            "name": "Example Candidate",
            "headline": "Backend Engineer",
            "summary": "Builds reliable backend systems.",
            "contact": {"email": "candidate@example.com", "phone": ""},
            "location": {"country_code": "TR", "country_name": "Türkiye", "city": "Istanbul"},
            "skills": {"Languages": ["Python", "Go"]},
            "experience": [],
            "projects": [],
            "education": [],
            "languages": [],
        },
        "mobility": {
            "schema_version": "1.0",
            "current_country_code": "TR",
            "work_authorizations": ["TR"],
            "relocation_targets": [{"country_code": "DE", "country_name": "Germany", "cities": []}],
        },
        "preferences": {
            "schema_version": "1.0",
            "target_roles": ["Backend Engineer"],
            "preferred_skills": ["Python", "Go"],
        },
        "enabled_source_ids": [],
        "llm": {"mode": "rules", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"},
        "activate": True,
    }


def configured_settings(tmp_path) -> Settings:
    return SetupService(Settings.load(tmp_path)).complete(setup_payload())


def test_incomplete_setup_redirects_and_sync_is_blocked(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        dashboard = client.get("/", follow_redirects=False)
        status = client.get("/api/setup/status")
        sync = client.post("/api/sync")

    assert dashboard.status_code == 307
    assert dashboard.headers["location"] == "/setup"
    assert status.json()["completed"] is False
    assert sync.status_code == 409


def test_idle_refresh_panel_stays_hidden_until_user_starts_refresh(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        page = client.get("/jobs")

    assert 'state.phase === "idle" || !state.started_at' in page.text
    assert "syncPanel.hidden = true" in page.text


def test_setup_exposes_validated_accessible_score_distribution(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        page = client.get("/setup")

    assert "Opportunity-fit point distribution" in page.text
    assert 'id="weight-location-authorization"' in page.text
    assert 'id="score-weight-total" role="status" aria-live="polite"' in page.text
    assert "Score weights must be non-negative whole numbers totaling 100" in page.text
    assert "a model never supplies it" in page.text


def test_manual_refresh_requires_explicit_activation(tmp_path, monkeypatch) -> None:
    payload = setup_payload()
    payload["activate"] = False
    app = create_app(SetupService(Settings.load(tmp_path)).complete(payload))
    calls: list[tuple[bool, bool]] = []

    async def run(force: bool = False, manual: bool = False):
        calls.append((force, manual))
        return app.state.sync_service.status

    monkeypatch.setattr(app.state.sync_service, "run", run)
    with TestClient(app) as client:
        response = client.post("/api/sync")

    assert response.status_code == 409
    assert calls == []


def test_setup_schema_validation_and_completion(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        schema = client.get("/api/schemas/candidate-profile")
        validation = client.post("/api/setup/profile/validate", json=setup_payload()["candidate"])
        setup_validation = client.post("/api/setup/validate", json=setup_payload())
        completion = client.post("/api/setup/complete", json=setup_payload())

    assert schema.status_code == 200
    assert schema.json()["candidate"]["title"] == "CandidateProfileV1"
    assert validation.json()["valid"] is True
    assert setup_validation.json()["valid"] is True
    assert "api_key" not in setup_validation.json()["payload"]["llm"]
    assert completion.json()["completed"] is True


def test_optional_company_search_key_is_stored_only_in_private_secrets(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        saved = client.post("/api/setup/company-search", json={"api_key": "brave-secret"})
        page = client.get("/settings")

    assert saved.json() == {"configured": True, "provider": "brave"}
    assert "brave-secret" not in page.text
    secrets = json.loads(app.state.settings.secrets_path.read_text(encoding="utf-8"))
    assert secrets["brave_search_api_key"] == "brave-secret"
    assert app.state.settings.secrets_path.read_text(encoding="utf-8").find("candidate@example.com") == -1


def test_setup_completion_refreshes_active_llm_settings(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))
    payload = setup_payload()
    payload["llm"] = {
        "mode": "ollama",
        "base_url": "http://model.lan:11434/v1",
        "model": "qwen3:14b",
        "api_key": "",
    }

    with TestClient(app) as client:
        completion = client.post("/api/setup/complete", json=payload)

    assert completion.json()["activated"] is True
    assert app.state.settings.llm_enabled is True
    assert app.state.settings.llm_mode == "ollama"
    assert app.state.settings.llm_model == "qwen3:14b"
    assert app.state.sync_service.llm.settings.llm_model == "qwen3:14b"


def test_llm_client_does_not_send_an_empty_bearer_token(tmp_path) -> None:
    client = LlmClient(Settings.load(tmp_path))

    assert client._headers() == {"Content-Type": "application/json"}


def test_switching_to_rules_refreshes_every_runtime_service(tmp_path) -> None:
    initial = setup_payload()
    initial["llm"] = {"mode": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "missing-model"}
    app = create_app(SetupService(Settings.load(tmp_path)).complete(initial))

    with TestClient(app) as client:
        completion = client.post("/api/setup/complete?return_to=/settings", json=setup_payload())

    assert completion.status_code == 200
    assert app.state.settings.llm_enabled is False
    assert app.state.sync_service.settings.llm_enabled is False
    assert app.state.sync_service.llm.settings.llm_enabled is False
    assert app.state.setup_service.settings.llm_mode == "rules"


def test_llm_score_total_is_derived_from_dimensions(tmp_path) -> None:
    value = {
        "total": 99,
        "dimensions": {
            "role_domain": 20,
            "stack": 15,
            "domain_experience": 10,
            "seniority": 8,
            "salary_employment": 5,
        },
        "confidence": 80,
        "evidence": [],
        "gaps": [],
    }
    eligibility = EligibilityResult(
        status=EligibilityStatus.ELIGIBLE, route="authorized-tr", sponsorship="unavailable",
        relocation="unknown", location_fit="authorized:TR", reasons=[], risks=[], threshold=65,
    )
    LlmClient._normalize_score(value, eligibility)
    LlmClient._validate_score(value)

    # location_authorization is never model-supplied - it's spliced in deterministically from
    # the eligibility status (15 for ELIGIBLE), never left to the model to guess.
    assert value["dimensions"]["location_authorization"] == 15
    assert value["total"] == 73
    assert value["confidence"] == 0.8
    assert value["verdict"] == "review"


def test_custom_score_weights_normalize_and_validate_against_the_same_distribution() -> None:
    preferences = {
        "score_weights": {
            "role_domain": 35,
            "stack": 20,
            "domain_experience": 10,
            "seniority": 15,
            "location_authorization": 15,
            "salary_employment": 5,
        }
    }
    value = {
        "dimensions": {
            "role_domain": 30,
            "stack": 20,
            "domain_experience": 10,
            "seniority": 15,
            "salary_employment": 10,
        },
        "confidence": 0.8,
        "evidence": [],
        "gaps": [],
    }
    eligibility = EligibilityResult(
        status=EligibilityStatus.ELIGIBLE,
        route="authorized-tr",
        sponsorship="unknown",
        relocation="unknown",
        location_fit="authorized:TR",
        reasons=[],
        risks=[],
    )

    LlmClient._normalize_score(value, eligibility, preferences)
    LlmClient._validate_score(value, preferences)

    assert value["dimensions"]["role_domain"] == 35
    assert value["dimensions"]["salary_employment"] == 5
    assert value["total"] == 100


def test_llm_rubric_uses_full_point_ranges_and_positive_evidence() -> None:
    assert "not 0-to-1 ratings" in SCORING_RUBRIC
    assert "role_domain (0-30)" in SCORING_RUBRIC
    assert 'never write "absent"' in SCORING_RUBRIC.casefold()


def test_llm_semantic_validation_rejects_negative_evidence_and_generic_gaps() -> None:
    value = {
        "dimensions": {
            "role_domain": 0,
            "stack": 0,
            "domain_experience": 10,
            "seniority": 8,
            "salary_employment": 5,
        },
        "evidence": [
            {
                "requirement": "stack",
                "profile_evidence": "Candidate knows Java, does not have React experience.",
            }
        ],
        "gaps": [
            {"requirement": "stack", "severity": "high"},
            {"requirement": "stack", "severity": "high"},
        ],
    }

    with pytest.raises(ValueError, match="zero-score dimension stack"):
        LlmClient._validate_score_semantics(value)


def test_llm_semantic_validation_accepts_but_rejects_contradicting_and_ungrounded_evidence() -> None:
    base_dimensions = {
        "role_domain": 20, "stack": 15, "domain_experience": 5, "seniority": 10, "salary_employment": 5,
    }

    # "but" alone is no longer a negation marker - it was the single biggest cause of the
    # 20% hard-failure rate measured against real jobs, and "but" mid-sentence is not reliably
    # negative ("Built Kafka pipelines, but at smaller scale" is still positive evidence).
    LlmClient._validate_score_semantics(
        {
            "dimensions": base_dimensions,
            "evidence": [{"requirement": "Kafka", "profile_evidence": "Built Kafka pipelines, but at smaller scale"}],
            "gaps": [],
        },
        {"kafka", "pipelines"},
    )

    with pytest.raises(ValueError, match="claimed as both evidence and a gap"):
        LlmClient._validate_score_semantics(
            {
                "dimensions": base_dimensions,
                "evidence": [{"requirement": "Kafka", "profile_evidence": "Built Kafka pipelines"}],
                "gaps": [{"requirement": "Kafka", "severity": "high"}],
            },
            {"kafka", "pipelines"},
        )

    with pytest.raises(ValueError, match="not grounded in the candidate profile"):
        LlmClient._validate_score_semantics(
            {
                "dimensions": base_dimensions,
                "evidence": [{"requirement": "Kafka", "profile_evidence": "Built Kafka pipelines"}],
                "gaps": [],
            },
            {"python", "django"},
        )


def test_normalize_score_overwrites_a_model_supplied_location_authorization() -> None:
    # Even if a model ignores the schema and sneaks the key back in, the deterministic value
    # must win - this dimension is never the model's call.
    value = {
        "dimensions": {
            "role_domain": 20, "stack": 15, "domain_experience": 5, "seniority": 10,
            "location_authorization": 999, "salary_employment": 5,
        },
        "confidence": 0.5,
        "evidence": [],
        "gaps": [],
    }
    eligibility = EligibilityResult(
        status=EligibilityStatus.UNKNOWN, route="other", sponsorship="unknown",
        relocation="unknown", location_fit="unknown", reasons=[], risks=[],
    )

    LlmClient._normalize_score(value, eligibility)

    assert value["dimensions"]["location_authorization"] == 8
    assert value["total"] == 63


def test_custom_location_weight_still_uses_only_deterministic_eligibility() -> None:
    value = {
        "dimensions": {
            "role_domain": 20, "stack": 15, "domain_experience": 5, "seniority": 10,
            "location_authorization": 999, "salary_employment": 5,
        },
        "confidence": 0.5, "evidence": [], "gaps": [],
    }
    eligibility = EligibilityResult(
        status=EligibilityStatus.UNKNOWN, route="other", sponsorship="unknown",
        relocation="unknown", location_fit="unknown", reasons=[], risks=[],
    )
    preferences = {
        "score_weights": {
            "role_domain": 25, "stack": 15, "domain_experience": 10,
            "seniority": 10, "location_authorization": 30, "salary_employment": 10,
        }
    }

    LlmClient._normalize_score(value, eligibility, preferences)

    assert value["dimensions"]["location_authorization"] == 16


@pytest.mark.asyncio
async def test_llm_score_retries_with_specific_semantic_feedback(tmp_path) -> None:
    invalid = {
        "dimensions": {
            "role_domain": 0, "stack": 0, "domain_experience": 0, "seniority": 0, "salary_employment": 5,
        },
        "confidence": 0.2,
        "evidence": [{"requirement": "stack", "profile_evidence": "Java, does not have React experience"}],
        "gaps": [{"requirement": "stack", "severity": "high"}],
    }
    corrected = {
        "dimensions": {
            "role_domain": 25, "stack": 10, "domain_experience": 5, "seniority": 10, "salary_employment": 5,
        },
        "confidence": 0.2,
        "evidence": [
            {"requirement": "TypeScript", "profile_evidence": "Candidate profile lists TypeScript among skills"}
        ],
        "gaps": [{"requirement": "React", "severity": "high"}],
    }

    class CorrectingClient(LlmClient):
        def __init__(self) -> None:
            super().__init__(Settings.load(tmp_path))
            self.calls: list[list[dict[str, str]]] = []

        async def _chat_content(self, messages, *_args, **_kwargs) -> str:
            self.calls.append(messages)
            return json.dumps(invalid if len(self.calls) == 1 else corrected)

    client = CorrectingClient()
    score = await client.score(
        {"title": "Frontend Engineer", "description": "React", "remote_scope": "Worldwide"},
        EligibilityResult(
            status=EligibilityStatus.ELIGIBLE,
            route="remote-from-tr",
            sponsorship="not_required",
            relocation="not_required",
            location_fit="remote:TR",
            reasons=[],
            risks=[],
        ),
        {},
        {"location": {"country_code": "TR", "country_name": "Türkiye"}, "skills": {"Languages": ["TypeScript"]}},
    )

    assert score.gaps == [{"requirement": "React", "severity": "high"}]
    assert len(client.calls) == 2
    assert "previous JSON is invalid" in client.calls[1][-1]["content"]


def test_qwen3_prompts_disable_thinking_for_structured_output(tmp_path) -> None:
    settings = SetupService(Settings.load(tmp_path)).complete(setup_payload())
    settings = replace(settings, llm_model="qwen3:14b")

    assert LlmClient(settings)._prompt_for_model("Return JSON").endswith("/no_think")


def test_ollama_native_payload_uses_json_schema_and_context_length(tmp_path) -> None:
    settings = replace(Settings.load(tmp_path), llm_mode="ollama", llm_enabled=True, llm_model="qwen3:14b")
    schema = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}

    payload = LlmClient(settings)._ollama_payload(
        [{"role": "user", "content": "Return JSON"}], schema, temperature=0.1, max_tokens=900
    )

    # No "think" key: forcing it off previously collapsed qwen3 scores to near-zero. Omitting it
    # lets Ollama use each model's own default thinking behavior.
    assert "think" not in payload
    assert payload["format"] == schema
    assert payload["options"]["num_predict"] == 4096
    assert payload["options"]["num_ctx"] == 16384


def test_ollama_native_payload_forces_thinking_off_for_qwen3_6(tmp_path) -> None:
    settings = replace(Settings.load(tmp_path), llm_mode="ollama", llm_enabled=True, llm_model="qwen3.6:27b")
    schema = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}

    payload = LlmClient(settings)._ollama_payload(
        [{"role": "user", "content": "Return JSON"}], schema, temperature=0.1, max_tokens=900
    )

    # Opposite of qwen3:14b above: left at its own default, qwen3.6 reasons long enough to exceed
    # llm_timeout_seconds outright, but scores as well as the recommended model with think forced
    # off. Scoped to "qwen3.6" specifically since qwen3:14b measurably got worse this way.
    assert payload["think"] is False


def test_setup_shows_searchable_country_catalog_and_rules_model_status(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        setup = client.get("/setup")
        model_status = client.get("/api/model/status")

    assert setup.status_code == 200
    assert 'data-country-code="TR" data-country-name="Türkiye"' in setup.text
    assert 'data-country-code="DE" data-country-name="Germany"' in setup.text
    assert "only sees jobs from your enabled sources" in setup.text
    assert model_status.json()["status"] == "rules_only"


def test_setup_validation_rejects_unknown_iso_country_codes(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))
    invalid = setup_payload()
    invalid["mobility"]["relocation_targets"][0]["country_code"] = "ZZ"

    with TestClient(app) as client:
        response = client.post("/api/setup/validate", json=invalid)

    assert response.json()["valid"] is False
    assert "ISO 3166-1" in str(response.json()["errors"])


def test_setup_validation_allows_europe_as_a_relocation_region_only(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))
    payload = setup_payload()
    payload["mobility"]["relocation_targets"] = [{"country_code": "EUROPE", "country_name": "Europe"}]

    with TestClient(app) as client:
        accepted = client.post("/api/setup/validate", json=payload)
        payload["mobility"]["work_authorizations"] = ["EUROPE"]
        rejected = client.post("/api/setup/validate", json=payload)

    assert accepted.json()["valid"] is True
    assert rejected.json()["valid"] is False
    assert "ISO 3166-1" in str(rejected.json()["errors"])


def test_setup_planning_requires_an_enabled_model(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/setup/plan",
            json={"candidate": setup_payload()["candidate"], "notes": "Remote from Türkiye", "llm": {"mode": "rules"}},
        )

    assert response.status_code == 409
    assert "Choose Ollama" in response.json()["detail"]


def test_dashboard_jobs_api_and_feedback(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    app = create_app(settings)
    database = app.state.database
    job_id, _ = database.upsert_job(
        CollectedJob(
            source="fixture",
            source_job_id="1",
            title="Backend Engineer",
            company="Example",
            location="Remote Worldwide",
            description="Build Python distributed systems",
            url="https://example.com/jobs/1",
            published_at=datetime.now(UTC),
        )
    )

    with TestClient(app) as client:
        dashboard = client.get("/")
        jobs = client.get("/api/jobs")
        feedback = client.post(f"/api/jobs/{job_id}/feedback", json={"status": "bookmarked"})

    assert dashboard.status_code == 200
    assert "RoleBeacon" in dashboard.text
    # An exact title-and-skill match against the fixture profile clears the 65-point recommended
    # bar (role_domain 30 + stack 5 + seniority 10 + location 15 + salary 5 = 65), so it shows here
    # rather than in the empty state.
    assert "Backend Engineer" in dashboard.text
    assert "No recommended jobs yet" not in dashboard.text
    assert "only sees jobs from your enabled sources" in dashboard.text
    assert jobs.json()["jobs"][0]["title"] == "Backend Engineer"
    assert feedback.json()["status"] == "bookmarked"
    assert database.get_job(job_id)["status"] == "bookmarked"


def test_job_detail_renders_mojibake_repair_filter_end_to_end(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="fixture", source_job_id="job-detail", title="Backend Engineer", company="Example",
            location="Remote Worldwide",
            description=(
                "<h2>About the role</h2><p>Lemon.io â€” build systems.</p>"
                "<h3>Responsibilities</h3><ul><li>Own backend services</li><li>Review designs</li></ul>"
                "<script>alert('unsafe')</script>"
            ),
            url="https://example.test/jobs/1",
            published_at=datetime.now(UTC),
        )
    )

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "<h3>About the role</h3>" in response.text
    assert "Lemon.io — build systems." in response.text
    assert "<li>Own backend services</li>" in response.text
    assert "unsafe" not in response.text
    assert "No filter named" not in response.text


def test_preferences_page_edits_the_complete_saved_setup_without_resetting_it(tmp_path) -> None:
    payload = setup_payload()
    payload["mobility"]["relocation_targets"].append({"country_code": "EUROPE", "country_name": "Europe"})
    payload["preferences"].update(
        {
            "priority_companies": ["Google", "Microsoft"],
            "company_watchlist": ["Cloudflare"],
            "company_blocklist": ["Example Bad Co"],
            "preferred_domains": ["distributed systems"],
        }
    )
    payload["llm"] = {"mode": "custom", "base_url": "http://model.example/v1", "model": "test-model"}
    settings = SetupService(Settings.load(tmp_path)).complete(payload)
    app = create_app(settings)
    updated = setup_payload()
    updated["mobility"]["relocation_targets"] = [{"country_code": "EUROPE", "country_name": "Europe"}]
    updated["preferences"].update(payload["preferences"])
    updated["llm"] = payload["llm"]

    with TestClient(app) as client:
        page = client.get("/settings")
        saved = client.post("/api/setup/complete?return_to=/settings", json=updated)

    assert page.status_code == 200
    assert "Edit your RoleBeacon preferences" in page.text
    assert 'data-complete-url="/api/setup/complete?return_to=/settings"' in page.text
    assert '"country_code": "EUROPE"' in page.text
    assert "Google" in page.text and "Cloudflare" in page.text
    assert saved.json()["redirect"] == "/settings"
    assert app.state.settings.load_mobility_profile()["relocation_targets"][0]["country_code"] == "EUROPE"
    assert app.state.settings.load_search_profile()["priority_companies"] == ["Google", "Microsoft"]


def test_source_filter_options_collapses_generated_rows_but_lists_others_individually() -> None:
    sources = [
        # A row can carry a stale per-country name left over from an older save (save_sources()
        # keeps an existing row's saved name forever) - the grouped label must not leak it.
        SourceConfig(id="google-careers-germany", kind="google_careers", name="Google Careers — Germany"),
        SourceConfig(id="google-careers-france", kind="google_careers", name="Google Careers"),
        SourceConfig(id="amazon-jobs-germany", kind="amazon_jobs", name="Amazon Jobs"),
        SourceConfig(id="adzuna-de", kind="adzuna", name="Adzuna — Germany"),
        SourceConfig(id="adzuna-uk", kind="adzuna", name="Adzuna — UK"),
    ]

    options = _source_filter_options(sources)

    assert options == [
        {"value": "google_careers", "label": "Google Careers"},
        {"value": "amazon_jobs", "label": "Amazon Jobs"},
        {"value": "adzuna-de", "label": "Adzuna — Germany"},
        {"value": "adzuna-uk", "label": "Adzuna — UK"},
    ]


def test_jobs_page_source_filter_groups_google_across_countries_and_company_list_narrows_watchlist(tmp_path) -> None:
    from rolebeacon.source_discovery import relocation_source_candidates

    payload = setup_payload()
    payload["preferences"]["company_watchlist"] = ["Watched Co"]
    settings = SetupService(Settings.load(tmp_path)).complete(payload)
    generated, _ = settings.save_sources(
        relocation_source_candidates([{"code": "DE", "name": "Germany"}, {"code": "FR", "name": "France"}])
    )
    germany = next(item for item in generated if item.kind == "google_careers" and "Germany" in item.url)
    france = next(item for item in generated if item.kind == "google_careers" and "France" in item.url)
    app = create_app(settings)
    database = app.state.database
    database.upsert_job(CollectedJob(
        source=germany.id, source_job_id="1", title="Backend Engineer", company="Example DE",
        location="Berlin", description="Build backend systems.", url="https://example.com/jobs/de",
        published_at=datetime.now(UTC),
    ))
    database.upsert_job(CollectedJob(
        source=france.id, source_job_id="2", title="Backend Engineer", company="Example FR",
        location="Paris", description="Build backend systems.", url="https://example.com/jobs/fr",
        published_at=datetime.now(UTC),
    ))
    database.upsert_job(CollectedJob(
        source="adzuna-de", source_job_id="3", title="Backend Engineer", company="Watched Co",
        location="Berlin", description="Build backend systems.", url="https://example.com/jobs/watched",
        published_at=datetime.now(UTC),
    ))

    with TestClient(app) as client:
        grouped = client.get("/jobs?source=google_careers")
        watchlist = client.get("/jobs?company_list=watchlist")

    assert grouped.status_code == 200
    assert grouped.text.count('value="google_careers"') == 1
    assert "Example DE" in grouped.text and "Example FR" in grouped.text
    assert watchlist.status_code == 200
    assert "Watched Co" in watchlist.text
    assert "Example DE" not in watchlist.text


def test_jobs_page_supports_page_size_and_location_filter_and_company_suggest(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    database = app.state.database
    for index in range(12):
        database.upsert_job(CollectedJob(
            source="fixture", source_job_id=str(index), title=f"Engineer {index}",
            company="Canonical" if index == 0 else "Other Co",
            location="Berlin" if index == 0 else "Paris",
            description="Build systems.", url=f"https://example.test/jobs/{index}",
            published_at=datetime.now(UTC),
        ))

    with TestClient(app) as client:
        paged = client.get("/jobs?page_size=10")
        unpaged = client.get("/jobs")
        located = client.get("/jobs?location=Berlin")
        suggestions = client.get("/api/companies/suggest?q=Canon")

    assert paged.status_code == 200
    assert "Page 1 of 2" in paged.text
    assert unpaged.status_code == 200
    assert "Page 1 of 2" not in unpaged.text
    assert located.status_code == 200
    assert "Engineer 0" in located.text
    assert "Engineer 1" not in located.text
    assert suggestions.status_code == 200
    assert suggestions.json()["companies"] == ["Canonical"]


def test_invalid_feedback_is_rejected(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/jobs/1/feedback", json={"status": "submitted_by_bot"})

    assert response.status_code == 422


def test_preferences_separate_search_from_application_and_hide_rules_details(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        page = client.get("/settings")

    assert page.status_code == 200
    assert 'data-settings-tab="search"' in page.text
    assert 'data-settings-tab="application"' in page.text
    assert "LLM fit may use summary, location, experience, projects, skills, education" in page.text
    assert "contact details are excluded" in page.text
    assert 'id="model-details"' in page.text
    assert 'modelDetails.hidden = document.getElementById("llm-mode").value === "rules"' in page.text
    assert 'message("Preferences saved.", true)' in page.text
    assert "Arbeitnow roles that explicitly advertise visa sponsorship" in page.text
    assert "Searches every country you're authorized to work in or willing to relocate to" in page.text
    assert "Remote-eligible search covering roles with no fixed country" in page.text


def test_settings_round_trip_preserves_omitted_fields_and_saved_api_key(tmp_path) -> None:
    payload = setup_payload()
    payload["mobility"].update({
        "relocation_targets": [{"country_code": "DE", "country_name": "Germany", "cities": ["Berlin"]}],
        "sponsorship_required_outside_authorized_countries": False,
        "timezone": "Europe/Istanbul",
    })
    payload["preferences"].update({
        "salary": {"minimum": 120000, "currency": "EUR", "hard_filter": True},
        "daily_review_limit": 7,
    })
    payload["llm"] = {
        "mode": "custom", "base_url": "http://model.test/v1", "model": "test",
        "api_key": "secret-value", "api_key_action": "replace",
    }
    service = SetupService(Settings.load(tmp_path))
    service.complete(payload)

    updated = service.complete({"candidate": {"headline": "Updated headline"}})

    assert updated.llm_api_key == "secret-value"
    assert updated.load_mobility_profile()["relocation_targets"][0]["cities"] == ["Berlin"]
    assert updated.load_mobility_profile()["timezone"] == "Europe/Istanbul"
    assert updated.load_mobility_profile()["sponsorship_required_outside_authorized_countries"] is False
    assert updated.load_search_profile()["salary"] == {"minimum": 120000.0, "currency": "EUR", "hard_filter": True}
    assert updated.load_search_profile()["daily_review_limit"] == 7


def test_job_detail_score_factors_are_keyboard_expandable_and_mode_transparent(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    job_id, _ = database.upsert_job(CollectedJob(
        source="fixture", source_job_id="tooltip", title="Backend Engineer", company="Example",
        location="Remote", description="Build systems", url="https://example.test/jobs/tooltip",
    ))
    database.save_evaluation(job_id, EligibilityResult(
        status=EligibilityStatus.UNKNOWN, route="other", sponsorship="unknown", relocation="unknown",
        location_fit="remote-scope-unknown", reasons=[], risks=["Scope unknown"],
    ), ScoreResult(
        total=30, dimensions={"role_domain": 10, "stack": 0, "domain_experience": 0, "seniority": 7, "location_authorization": 8, "salary_employment": 5},
        confidence=.5, verdict="low_priority", evidence=[], gaps=[], provider="rules", model="deterministic",
    ), "scored")

    with TestClient(create_app(settings)) as client:
        page = client.get(f"/jobs/{job_id}")

    assert page.text.count('class="score-factor"') == 6
    assert 'data-score-factor="location_authorization"' in page.text
    assert "Missing evidence does not prove you lack the qualification" in page.text
    assert "no model can set or override it" in page.text
    assert ".score-factor summary:focus-visible" in (settings.resource_dir / "static" / "style.css").read_text()


def test_refresh_completion_step_and_partial_error_state_are_distinct(tmp_path) -> None:
    with TestClient(create_app(configured_settings(tmp_path))) as client:
        page = client.get("/")

    assert 'if (phase === "complete") return "complete"' in page.text
    assert 'phase === "completed_with_errors"' in page.text


def test_source_preview_requires_activation_and_origin_matches_scheme_and_port(tmp_path) -> None:
    payload = setup_payload()
    payload["activate"] = False
    app = create_app(SetupService(Settings.load(tmp_path)).complete(payload))
    with TestClient(app) as client:
        preview = client.post("/api/sources/discover", json={"careers_url": "https://boards.greenhouse.io/example"})
        hostile = client.post("/api/setup/validate", json={}, headers={"Origin": "http://localhost:9999"})

    assert preview.status_code == 409
    assert hostile.status_code == 403


def test_json_mutations_reject_non_object_payloads(tmp_path) -> None:
    with TestClient(create_app(Settings.load(tmp_path))) as client:
        response = client.post("/api/setup/validate", json=["not", "an", "object"])

    assert response.status_code == 422


def test_mutations_reject_string_boolean_and_invalid_integer_coercion(tmp_path) -> None:
    with TestClient(create_app(configured_settings(tmp_path))) as client:
        source = client.post(
            "/api/sources",
            json={"careers_url": "https://boards.greenhouse.io/example", "enabled": "false"},
        )
        duplicate = client.post("/api/duplicates/1/merge", json={"keep_job_id": "not-an-integer"})

    assert source.status_code == 422
    assert duplicate.status_code == 422


def test_manual_import_rejects_non_web_url_schemes(tmp_path) -> None:
    with TestClient(create_app(configured_settings(tmp_path))) as client:
        response = client.post("/api/imports", json={"title": "Role", "company": "Example", "url": "javascript:alert(1)"})

    assert response.status_code == 422


def test_realistic_job_detail_with_llm_evidence_renders_without_500(tmp_path) -> None:
    from rolebeacon.domain import ScoreResult

    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="remoteok", source_job_id="162", title="Senior React Full stack Developer",
            company="Lemon.io", location="Remote Worldwide", description="Build software worldwide.",
            url="https://example.test/jobs/162",
        )
    )
    eligibility = EligibilityResult(
        status=EligibilityStatus.ELIGIBLE, route="remote-tr", sponsorship="unknown", relocation="unknown",
        location_fit="worldwide", reasons=["Worldwide remote"], risks=[],
    )
    app.state.database.save_evaluation(
        job_id, eligibility,
        ScoreResult(
            total=70, dimensions={}, confidence=0.8, verdict="review",
            evidence=[{"requirement": "Relevant skills", "profile_evidence": "Go, Java, Python"}], gaps=[],
            provider="openai-compatible", model="qwen3:14b", prompt_version="test",
        ), "scored",
    )

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "Senior React Full stack Developer" in response.text
    assert "Why it matches" in response.text


def test_job_detail_shows_the_ineligible_score_cap_explanation(tmp_path) -> None:
    from rolebeacon.domain import ScoreResult
    from rolebeacon.scoring import INELIGIBLE_SCORE_CAP, SCORING_PROMPT_VERSION, scoring_behavior_version

    # Startup runs an immediate sync, which requeues and rescores any job whose stored
    # prompt_version doesn't match the current one - match it so this seeded score survives.
    scoring_version = f"{SCORING_PROMPT_VERSION}:rules:weights-{scoring_behavior_version()}"
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="ineligible", title="Backend Engineer", company="Example",
            location="United States", description="Requires US citizenship, no sponsorship offered.",
            url="https://example.test/jobs/ineligible",
        )
    )
    eligibility = EligibilityResult(
        status=EligibilityStatus.INELIGIBLE, route="", sponsorship="unavailable", relocation="unknown",
        location_fit="onsite", reasons=[], risks=["Citizens only, sponsorship unavailable"],
    )
    app.state.database.save_evaluation(
        job_id, eligibility,
        ScoreResult(
            total=INELIGIBLE_SCORE_CAP, dimensions={}, confidence=0.8, verdict="reject",
            evidence=[], gaps=[{"requirement": "Citizens only, sponsorship unavailable", "severity": "high"}],
            provider="rules", model="deterministic-v2", prompt_version=scoring_version,
        ), "scored",
    )

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert f"Score capped at {INELIGIBLE_SCORE_CAP}" in response.text


def test_job_detail_shows_the_score_breakdown_by_dimension(tmp_path) -> None:
    from rolebeacon.domain import ScoreResult
    from rolebeacon.scoring import SCORING_PROMPT_VERSION, scoring_behavior_version

    # Startup runs an immediate sync, which requeues and rescores any job whose stored
    # prompt_version doesn't match the current one - match it so this seeded score survives.
    scoring_version = f"{SCORING_PROMPT_VERSION}:rules:weights-{scoring_behavior_version()}"
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="scored", title="Backend Engineer", company="Example",
            location="Remote Worldwide", description="Build backend systems.",
            url="https://example.test/jobs/scored",
        )
    )
    eligibility = EligibilityResult(
        status=EligibilityStatus.ELIGIBLE, route="remote-tr", sponsorship="unknown", relocation="unknown",
        location_fit="worldwide", reasons=[], risks=[],
    )
    app.state.database.save_evaluation(
        job_id, eligibility,
        ScoreResult(
            total=85,
            dimensions={
                "role_domain": 25, "stack": 15, "domain_experience": 10,
                "seniority": 10, "location_authorization": 15, "salary_employment": 10,
            },
            confidence=0.8, verdict="review", evidence=[], gaps=[],
            provider="rules", model="deterministic-v2", prompt_version=scoring_version,
        ), "scored",
    )

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "Score breakdown" in response.text
    assert "Role match" in response.text
    assert "25 / 30" in response.text
    assert "15 / 20" in response.text


def test_jobs_page_hides_mismatched_titles_by_default(tmp_path) -> None:
    from rolebeacon.domain import ScoreResult
    from rolebeacon.scoring import SCORING_PROMPT_VERSION

    # Startup runs an immediate sync, which requeues and rescores any job whose stored
    # prompt_version doesn't match the current one - match it so these seeded scores survive.
    scoring_version = f"{SCORING_PROMPT_VERSION}:rules"
    app = create_app(configured_settings(tmp_path))
    database = app.state.database
    matched_id, _ = database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="matched", title="Backend Engineer", company="Example",
            location="Remote Worldwide", description="Build backend systems.",
            url="https://example.test/jobs/matched",
        )
    )
    mismatched_id, _ = database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="mismatched", title="Talent Acquisition Specialist", company="Other Example",
            location="Remote Worldwide", description="Own the roadmap.",
            url="https://example.test/jobs/mismatched",
        )
    )
    eligibility = EligibilityResult(
        status=EligibilityStatus.ELIGIBLE, route="remote-tr", sponsorship="unknown", relocation="unknown",
        location_fit="worldwide", reasons=[], risks=[],
    )

    def evaluation(role_domain: int) -> ScoreResult:
        return ScoreResult(
            total=70, dimensions={"role_domain": role_domain}, confidence=0.8, verdict="review",
            evidence=[], gaps=[], provider="rules", model="deterministic-v2", prompt_version=scoring_version,
        )

    database.save_evaluation(matched_id, eligibility, evaluation(25), "scored")
    database.save_evaluation(mismatched_id, eligibility, evaluation(2), "scored")

    with TestClient(app) as client:
        default_view = client.get("/jobs")
        shown = client.get("/jobs?show_mismatched_titles=1")

    assert "Backend Engineer" in default_view.text
    assert "Talent Acquisition Specialist" not in default_view.text
    assert "1 of 2 jobs shown" in default_view.text
    assert "1 different-role title hidden" in default_view.text
    assert "Hiding different-role titles: 1 job" in default_view.text
    assert "show_mismatched_titles=1" in default_view.text
    assert "Backend Engineer" in shown.text
    assert "Talent Acquisition Specialist" in shown.text
    assert "2 of 2 jobs shown" in shown.text
    assert "Hiding different-role titles" not in shown.text


def test_sources_page_explains_a_quarantined_snapshot_drop(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    app = create_app(settings)
    source_id = settings.load_sources()[0].id
    app.state.database.upsert_job(
        CollectedJob(
            source=source_id,
            source_job_id="baseline-job",
            title="Backend Engineer",
            company="Example",
            location="Remote Worldwide",
            description="Build backend systems.",
            url="https://example.test/jobs/baseline-job",
        )
    )
    app.state.database.reconcile_source_snapshot_guarded(source_id, {"baseline-job"})
    result = app.state.database.reconcile_source_snapshot_guarded(source_id, set())

    with TestClient(app) as client:
        page = client.get("/sources")

    assert result.reconciled is False
    assert page.status_code == 200
    assert "Snapshot count fell from 1 to 0" in page.text
    assert "second consistent complete snapshot" in page.text


def test_job_detail_decisions_are_in_place_and_cover_letter_requires_llm(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="decision", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/decision",
        )
    )

    with TestClient(app) as client:
        page = client.get(f"/jobs/{job_id}")
        decision = client.post(f"/api/jobs/{job_id}/feedback", json={"status": "bookmarked"})

    assert page.status_code == 200
    assert "data-decision-form" in page.text
    assert "event.preventDefault()" in page.text
    assert "The tailored résumé uses only your locally stored candidate profile" in page.text
    assert "Cover letter requires an LLM" in page.text
    assert decision.status_code == 200
    assert app.state.database.get_job(job_id)["status"] == "bookmarked"


def test_job_detail_renders_the_cover_letter_draft_and_links_the_printable_version(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    app = create_app(settings)
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="cover-letter", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/cover-letter",
        )
    )
    directory = settings.data_dir / "applications" / str(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cover-letter.txt").write_text("Dear Hiring Team,\n\nA tailored paragraph.\n\nSincerely,\nExample Candidate\n", encoding="utf-8")
    (directory / "cover-letter.html").write_text("<p>Dear Hiring Team,</p>", encoding="utf-8")
    app.state.database.save_application(job_id, status="preparing", cover_letter_path=str(directory / "cover-letter.txt"))

    with TestClient(app) as client:
        page = client.get(f"/jobs/{job_id}")

    assert page.status_code == 200
    assert "A tailored paragraph." in page.text
    assert f"/artifacts/{job_id}/cover-letter.html" in page.text


def test_bookmarking_a_job_shows_it_on_the_pipeline_board_without_a_resume(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="bookmark", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/bookmark",
        )
    )

    with TestClient(app) as client:
        client.post(f"/api/jobs/{job_id}/feedback", json={"status": "bookmarked"})
        board = client.get("/applications")

    assert board.status_code == 200
    assert "Job tracking" in board.text
    assert "Backend Engineer" in board.text
    assert not app.state.database.list_applications()


def test_job_detail_bookmark_button_toggles_between_bookmark_and_remove(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="toggle", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/toggle",
        )
    )

    with TestClient(app) as client:
        before = client.get(f"/jobs/{job_id}")
        client.post(f"/api/jobs/{job_id}/feedback", json={"status": "bookmarked"})
        after = client.get(f"/jobs/{job_id}")

    # Not bookmarked yet: the button offers to bookmark it.
    assert 'data-toggle="bookmarked" value="bookmarked"' in before.text
    assert ">Bookmark<" in before.text
    # Already bookmarked: the same button now offers to undo it, not re-send "bookmarked" forever.
    assert 'data-toggle="bookmarked" value="new"' in after.text
    assert ">Remove bookmark<" in after.text


def test_removing_a_bookmarked_job_takes_it_off_the_pipeline_board(tmp_path) -> None:
    # The job tracking page's delete affordance and the job-detail "Remove bookmark" toggle both
    # just POST status=new at the existing feedback endpoint - confirm that alone is enough to
    # drop the job off the board, without deleting the job itself.
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="unbookmark", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/unbookmark",
        )
    )

    with TestClient(app) as client:
        client.post(f"/api/jobs/{job_id}/feedback", json={"status": "bookmarked"})
        on_board = client.get("/applications")
        client.post(f"/api/jobs/{job_id}/feedback", json={"status": "new"})
        off_board = client.get("/applications")
        still_browsable = client.get(f"/jobs/{job_id}")

    assert "Backend Engineer" in on_board.text
    assert "Backend Engineer" not in off_board.text
    assert still_browsable.status_code == 200


def test_manual_import_does_not_fetch_and_creates_job(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/imports",
            json={
                "title": "Senior Platform Engineer",
                "company": "Example",
                "url": "https://does-not-need-to-exist.invalid/jobs/1",
                "location": "Remote Worldwide",
                "description": "Build distributed systems",
            },
        )

    assert response.status_code == 201
    assert app.state.database.get_job(response.json()["job_id"])["company"] == "Example"


def test_native_form_csrf_hidden_field_succeeds_without_header(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        page = client.get("/imports")
        token_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert token_match is not None
        response = client.post(
            "/api/imports",
            data={
                "csrf_token": token_match.group(1),
                "title": "Senior Platform Engineer",
                "company": "Example",
                "url": "https://example.test/jobs/form-import",
            },
            headers={"Sec-Fetch-Site": "same-origin", "Accept": "text/html"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert app.state.database.list_jobs()[0]["title"] == "Senior Platform Engineer"


def test_browser_csrf_rejection_renders_html_error_page(tmp_path) -> None:
    with TestClient(create_app(configured_settings(tmp_path))) as client:
        response = client.post(
            "/api/imports",
            data={"title": "Unsaved", "company": "Example", "url": "https://example.test/jobs/unsaved"},
            headers={"Sec-Fetch-Site": "same-origin", "Accept": "text/html"},
        )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "A valid CSRF token is required" in response.text
    assert not response.text.startswith("{")


def test_synthetic_testserver_origin_is_allowed_only_for_the_test_client_peer(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app, client=("network-peer", 50000)) as client:
        response = client.get("/", headers={"Accept": "text/html"})

    assert response.status_code == 403
    assert "configured local host" in response.text


def test_company_research_can_be_refreshed_from_the_company_profile(tmp_path, monkeypatch) -> None:
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="fixture",
            source_job_id="company-refresh",
            title="Backend Engineer",
            company="Example",
            location="Remote Worldwide",
            description="Build distributed backend systems with relocation support.",
            url="https://example.test/jobs/company-refresh",
            published_at=datetime.now(UTC),
        )
    )
    async def unavailable_registry(_name: str):
        return "", []
    monkeypatch.setattr(app.state.company_research, "_wikidata_entry", unavailable_registry)

    with TestClient(app) as client:
        headers = {"Accept": "text/html"}
        initial = client.post(f"/api/jobs/{job_id}/research-company", headers=headers, follow_redirects=False)
        company = app.state.database.list_companies()[0]
        refreshed = client.post(
            f"/api/companies/{company['id']}/research", headers=headers, follow_redirects=False
        )

    assert initial.status_code == 303
    assert refreshed.status_code == 303
    assert refreshed.headers["location"] == f"/companies/{company['id']}"


def test_company_research_progress_endpoint_completes_without_a_raw_error_page(tmp_path, monkeypatch) -> None:
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="fixture", source_job_id="company-progress", title="Backend Engineer", company="Example",
            location="Remote Worldwide", description="Build distributed backend systems.",
            url="https://example.test/jobs/company-progress", published_at=datetime.now(UTC),
        )
    )
    async def unavailable_registry(_name: str):
        return "", []
    monkeypatch.setattr(app.state.company_research, "_wikidata_entry", unavailable_registry)

    with TestClient(app) as client:
        started = client.post(f"/api/jobs/{job_id}/research-company/start")
        state = started.json()["status"]
        for _ in range(50):
            state = client.get("/api/company-research/status").json()
            if not state["running"]:
                break
            sleep(0.01)

    assert started.status_code == 202
    assert state["phase"] == "complete"
    assert state["company_id"] is not None
    assert not state["error"]
