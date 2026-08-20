from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import sleep

import pytest
from fastapi.testclient import TestClient

from rolebeacon.app import JOB_STATUS_LABELS, _source_filter_options, create_app
from rolebeacon.company import RULES_MODEL
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob, EligibilityResult, EligibilityStatus, ScoreResult, SourceConfig
from rolebeacon.llm import SCORING_RUBRIC, LlmClient
from rolebeacon.scoring import seniority_level_options
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


def test_setup_and_settings_pages_render_wizard_and_source_packs(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        page = client.get("/setup")
        settings_page = client.get("/settings")

    assert page.status_code == 200
    assert 'id="setup-wizard"' in page.text
    assert "Fill the important fields yourself" in page.text
    assert "Ask an LLM for setup JSON" in page.text
    assert "Curated source packs" in page.text
    assert "Developer infrastructure" in page.text
    assert 'class="source-pack-choice"' in page.text
    assert settings_page.status_code == 200
    assert 'id="setup-wizard"' not in settings_page.text
    assert "Edit your RoleBeacon preferences" in settings_page.text


def test_hidden_wizard_steps_cannot_leak_the_activate_and_save_bar(tmp_path) -> None:
    # .setup-actions sets display: flex, which outranks the hidden attribute the wizard uses to
    # switch steps. Without an explicit opt-out the final activate-and-save bar stays on screen
    # for every wizard step, and pressing it there dies in native validation of hidden required
    # inputs without ever reaching the submit handler.
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        page = client.get("/setup")
        stylesheet = client.get("/static/style.css")

    assert 'class="setup-actions wizard-panel" data-wizard-step="review"' in page.text
    assert ".wizard-panel[hidden] { display: none; }" in stylesheet.text


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
        # Both LinkedIn kinds read the same postings, so they collapse into one option together.
        SourceConfig(id="linkedin-europe", kind="linkedin", name="LinkedIn — Europe"),
        SourceConfig(id="linkedin-turkiye", kind="linkedin", name="LinkedIn — Türkiye"),
        SourceConfig(id="linkedin-browser-europe", kind="linkedin_browser", name="LinkedIn (signed in) — Europe"),
        SourceConfig(id="adzuna-de", kind="adzuna", name="Adzuna — Germany"),
        SourceConfig(id="adzuna-uk", kind="adzuna", name="Adzuna — UK"),
    ]

    options = _source_filter_options(sources)

    assert options == [
        {"value": "google_careers", "label": "Google Careers"},
        {"value": "amazon_jobs", "label": "Amazon Jobs"},
        {"value": "linkedin", "label": "LinkedIn"},
        {"value": "adzuna-de", "label": "Adzuna — Germany"},
        {"value": "adzuna-uk", "label": "Adzuna — UK"},
    ]


def test_the_linkedin_filter_covers_both_the_guest_and_the_signed_in_rows() -> None:
    """One "LinkedIn" option, every LinkedIn row behind it - the two kinds read the same postings."""
    from starlette.datastructures import QueryParams

    from rolebeacon.app import _job_filters_from_query

    sources = [
        SourceConfig(id="linkedin-europe", kind="linkedin", name="LinkedIn — Europe"),
        SourceConfig(id="linkedin-browser-europe", kind="linkedin_browser", name="LinkedIn (signed in) — Europe"),
        SourceConfig(id="adzuna-de", kind="adzuna", name="Adzuna — Germany"),
    ]

    filters = _job_filters_from_query(QueryParams("source=linkedin"), sources)

    assert set(filters.source_ids) == {"linkedin-europe", "linkedin-browser-europe"}


def test_each_linkedin_method_is_switched_as_a_whole_rather_than_row_by_row() -> None:
    """The Sources panel offers "public search" or "my own session", not one location at a time."""
    from rolebeacon.app import _linkedin_methods

    methods = _linkedin_methods([
        SourceConfig(id="linkedin-europe", kind="linkedin", name="LinkedIn — Europe", enabled=True),
        SourceConfig(id="linkedin-remote", kind="linkedin", name="LinkedIn — Remote", enabled=False),
        SourceConfig(
            id="linkedin-browser-europe", kind="linkedin_browser",
            name="LinkedIn (signed in) — Europe", enabled=False,
        ),
        SourceConfig(id="adzuna-de", kind="adzuna", name="Adzuna — Germany", enabled=True),
    ])

    assert methods["linkedin"]["ids"] == ["linkedin-europe", "linkedin-remote"]
    assert (methods["linkedin"]["enabled"], methods["linkedin"]["total"]) == (1, 2)
    assert (methods["linkedin_browser"]["enabled"], methods["linkedin_browser"]["total"]) == (0, 1)


def test_a_profile_with_no_generated_linkedin_rows_gets_no_linkedin_panel() -> None:
    from rolebeacon.app import _linkedin_methods

    assert _linkedin_methods([SourceConfig(id="adzuna-de", kind="adzuna", name="Adzuna")]) == {}


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
    assert "12 of 12 jobs match" in paged.text
    assert paged.text.count('class="job-card"') == 10
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
    settings = configured_settings(tmp_path)
    scoring_version = (
        f"{SCORING_PROMPT_VERSION}:rules:"
        f"weights-{scoring_behavior_version(settings.load_search_profile(), settings.load_candidate_profile())}"
    )
    app = create_app(settings)
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
    settings = configured_settings(tmp_path)
    scoring_version = (
        f"{SCORING_PROMPT_VERSION}:rules:"
        f"weights-{scoring_behavior_version(settings.load_search_profile(), settings.load_candidate_profile())}"
    )
    app = create_app(settings)
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
    assert "1 of 2 jobs match" in default_view.text
    assert "1 different-role title hidden" in default_view.text
    assert "Hiding different-role titles: 1 job" in default_view.text
    assert "show_mismatched_titles=1" in default_view.text
    assert "Backend Engineer" in shown.text
    assert "Talent Acquisition Specialist" in shown.text
    assert "2 of 2 jobs match" in shown.text
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
        app.state.database.finish_source(source_id, seen=1, changed=0, truncated=True)
        later_page = client.get("/sources")

    assert result.reconciled is False
    assert page.status_code == 200
    assert "Snapshot count fell from 1 to 0" in page.text
    assert "second consistent complete snapshot" in page.text
    assert "Snapshot count fell from 1 to 0" not in later_page.text
    assert "Coverage was incomplete; this run was not used to close missing jobs." in later_page.text


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
    # Rules-only mode cannot write a cover letter, so the control leads to the setting that can.
    assert "Set up a model to write cover letters" in page.text
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


def test_the_job_rail_states_the_pipeline_once_and_keeps_its_actions_together(tmp_path) -> None:
    # Five buttons for one field showed no indication of which one was already true, and the
    # rail then explained in prose where the primary action lived.
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

    # One control for one value, and it shows the value the job actually holds.
    for page, expected in ((before, "new"), (after, "bookmarked")):
        options = re.findall(r'<option value="([^"]+)"([^>]*)>', page.text)
        assert [value for value, _ in options] == list(JOB_STATUS_LABELS)
        assert [value for value, attributes in options if "selected" in attributes] == [expected]
    assert 'data-toggle="bookmarked"' not in after.text
    # The primary action sits with the other actions instead of being described from a distance.
    rail = after.text.split('<aside class="detail-sidebar">')[1]
    assert "data-prepare-form" in rail
    assert "open-browser-toggle" in rail
    assert "at the top of the page" not in after.text
    # An employer nobody has researched says so.
    assert "company fit not researched" in after.text


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


def test_seniority_facet_offers_every_level_the_scorer_reads(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        page = client.get("/jobs")

    # The facet used to be a hand-written list that had drifted: "mid" and "intern" are levels the
    # scorer recognises in a title but the filter offered no way to ask for.
    for level in seniority_level_options():
        assert f'name="seniority" value="{level["code"]}"' in page.text


def test_nav_marks_the_open_page_including_a_job_detail(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    database = Database(app.state.settings.database_path)
    job_id, _ = database.upsert_job(CollectedJob(
        source="source-a", source_job_id="job-1", title="Backend Engineer", company="Example",
        location="Remote", description="Java", url="https://example.com/jobs/1",
    ))

    with TestClient(app) as client:
        listing = client.get("/jobs")
        detail = client.get(f"/jobs/{job_id}")
        elsewhere = client.get("/sources")

    # A job detail still belongs to Jobs: the nav marks the section, not the exact URL.
    assert listing.text.count('aria-current="page"') == 1
    assert '<a href="/jobs" title="Browse collected jobs and refine your search" class="is-current"' in listing.text
    assert '<a href="/jobs" title="Browse collected jobs and refine your search" class="is-current"' in detail.text
    assert '<a href="/sources" title="Inspect collection source health and sync history" class="is-current"' in elsewhere.text


def test_a_job_opened_from_a_filtered_list_links_back_to_that_list(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    database = Database(app.state.settings.database_path)
    job_id, _ = database.upsert_job(CollectedJob(
        source="source-a", source_job_id="job-1", title="Backend Engineer", company="Example",
        location="Remote", description="Java", url="https://example.com/jobs/1",
    ))

    with TestClient(app) as client:
        listing = client.get("/jobs?work_model=remote&page_size=20")
        carried = client.get(f"/jobs/{job_id}?return=%2Fjobs%3Fseniority%3Dsenior")
        direct = client.get(f"/jobs/{job_id}")
        offsite = client.get(f"/jobs/{job_id}?return=https%3A%2F%2Fevil.example%2Fx")
        protocol_relative = client.get(f"/jobs/{job_id}?return=%2F%2Fevil.example%2Fx")

    assert "return=%2Fjobs%3Fwork_model%3Dremote%26page_size%3D20" in listing.text
    assert '<a class="back-link" href="/jobs?seniority=senior">← Back to results</a>' in carried.text
    assert '<a class="back-link" href="/jobs">← All jobs</a>' in direct.text
    # A return value is only ever a path on this site, so the link cannot be pointed off it.
    assert '<a class="back-link" href="/jobs">← All jobs</a>' in offsite.text
    assert '<a class="back-link" href="/jobs">← All jobs</a>' in protocol_relative.text


def test_a_job_card_states_its_facts_as_tags_not_sentences(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    database = Database(app.state.settings.database_path)
    database.upsert_job(CollectedJob(
        source="source-a", source_job_id="job-1", title="Backend Engineer", company="Example",
        location="Berlin, Germany", description="Java backend role", url="https://example.com/jobs/1",
        salary_min=95000, salary_max=130000, salary_currency="EUR",
        published_at=datetime.now(UTC) - timedelta(days=3),
    ))

    with TestClient(app) as client:
        page = client.get("/jobs")

    row = re.search(r'<div class="meta-row">(.*?)</div>', page.text, re.S).group(1)
    # The eligibility fact is a tag; its sentence stays reachable as the tag's own tooltip.
    assert ">sponsorship needed</span>" in row
    assert 'title="Would need sponsorship in Germany, but the posting does not confirm it."' in row
    assert "Would need sponsorship in Germany, but the posting does not confirm it.</span>" not in row
    # The strategy is named, not identified by its key.
    assert "Relocation to Germany" in row
    assert ">relocate-de<" not in row
    # Pay the posting states, and an age rather than a calendar date.
    assert "EUR 95,000-130,000" in row
    assert "3 days ago" in row
    assert ">2026-" not in row


def test_a_review_decision_returns_to_the_queue_and_refuses_an_off_site_return(tmp_path) -> None:
    # The review queue is only faster than the list if a decision can be made without leaving it.
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="review-decision", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/review-decision",
        )
    )

    with TestClient(app) as client:
        client.post(f"/api/jobs/{job_id}/feedback", json={"status": "bookmarked"})
        queue = client.get("/review")
        decision = client.post(
            f"/api/jobs/{job_id}/feedback",
            data={"status": "applied", "return": "/review?i=0"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        recorded = app.state.database.get_job(job_id)["status"]
        elsewhere = client.post(
            f"/api/jobs/{job_id}/feedback",
            data={"status": "new", "return": "//evil.test/steal"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

    # The decision is on the queue page itself, not one click away on the job detail.
    assert f'action="/api/jobs/{job_id}/feedback"' in queue.text
    assert ">I applied<" in queue.text
    assert decision.status_code == 303
    assert decision.headers["location"] == "/review?i=0"
    assert recorded == "applied"
    # A "return" that leaves this origin is dropped rather than followed.
    assert elsewhere.headers["location"] == f"/jobs/{job_id}"


def test_the_board_reads_as_a_pipeline_and_a_card_can_be_moved_without_a_mouse(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="board", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/board",
        )
    )

    with TestClient(app) as client:
        client.post(f"/api/jobs/{job_id}/feedback", json={"status": "bookmarked"})
        board = client.get("/applications")

    headings = re.findall(r'<section class="kanban-column" data-status="([^"]+)"', board.text)
    # Left to right in the order work moves, not alphabetically or by internal enum order.
    assert headings == ["bookmarked", "applied", "offer", "rejected", "not_interested"]
    # Dragging is not the only way to change a status.
    assert f'data-move-job="{job_id}"' in board.text
    assert '<option value="applied">Applied</option>' in board.text
    assert '<option value="new">Off the board</option>' in board.text
    # Every column is offered, so a card moved on the client still has a complete menu.
    assert '<option value="bookmarked">' in board.text
    # Route ids are labelled the same way they are everywhere else in the UI.
    card = re.search(r"<small>(.*?)</small>", board.text, re.S)
    assert card is not None
    labels = {str(item["label"]) for item in app.state.settings.load_strategies()} | {"Unclassified"}
    assert card.group(1).split(" · ")[1] in labels


def test_the_source_table_opens_on_what_needs_attention_and_can_be_searched(tmp_path) -> None:
    # 262 configured sources with no filter means finding the broken one costs 261 rows of
    # scrolling. The table opens on the ones a person has to do something about.
    # No startup refresh: the point of the test is the two outcomes recorded below, not whatever
    # a background sync would write over them.
    settings = replace(configured_settings(tmp_path), auto_sync=False)
    healthy, broken = settings.load_sources()[:2]
    for source in (healthy, broken):
        settings.set_source_enabled(source.id, True)
    app = create_app(settings)

    app.state.database.start_source(healthy.id)
    app.state.database.finish_source(healthy.id, seen=12, changed=1)
    app.state.database.start_source(broken.id)
    app.state.database.fail_source(broken.id, "connection refused")

    with TestClient(app) as client:
        page = client.get("/sources")

    rows = {
        search: state
        for state, search in re.findall(
            r'data-source-row\s+data-state="([^"]*)"\s+data-search="([^"]*)"', page.text
        )
    }
    assert page.status_code == 200
    # A source that ran and failed needs attention; one that ran cleanly does not.
    assert [state for search, state in rows.items() if f" {broken.id} " in search] == ["attention enabled"]
    assert [state for search, state in rows.items() if f" {healthy.id} " in search] == ["enabled"]
    # The filter controls exist and default to the sources that need attention.
    assert 'data-source-state="attention"' in page.text
    assert 'id="source-health-search"' in page.text
    assert 'let healthState = "attention"' in page.text
    # Timestamps are relative, with the exact instant kept in the title.
    assert ">2026-" not in page.text.split("SOURCE HEALTH")[1]


def test_a_job_card_badges_only_what_could_have_been_otherwise(tmp_path) -> None:
    # "Eligible" is what the default filter already guarantees and "new" is 99% of every list,
    # so badging either says nothing while drowning the verdict, which does vary.
    settings = replace(configured_settings(tmp_path), auto_sync=False)
    app = create_app(settings)
    job_id, _ = app.state.database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="badges", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/badges",
        )
    )
    app.state.database.save_evaluation(
        job_id,
        EligibilityResult(
            status=EligibilityStatus.ELIGIBLE, route="authorized-tr", sponsorship="unknown",
            relocation="unknown", location_fit="authorized:TR", reasons=[], risks=[],
        ),
        ScoreResult(
            total=80, dimensions={}, confidence=90, verdict="review", evidence=[], gaps=[],
            provider="rules", model="rules",
        ),
        "scored",
    )

    with TestClient(app) as client:
        default_list = client.get("/jobs")
        bookmarked = client.post(f"/api/jobs/{job_id}/feedback", json={"status": "bookmarked"})
        after = client.get("/jobs")

    badges = re.search(r'<div class="job-card-badges">(.*?)</div>', default_list.text, re.S)
    assert badges is not None
    assert ">eligible<" not in badges.group(1)
    assert ">new<" not in badges.group(1)
    assert ">review<" in badges.group(1)
    # A decision the reader actually made is still worth a badge.
    assert bookmarked.status_code == 200
    assert ">bookmarked<" in re.search(r'<div class="job-card-badges">(.*?)</div>', after.text, re.S).group(1)


def test_a_help_tip_never_steals_its_field_s_label(tmp_path) -> None:
    # An implicit <label> binds to its first labelable descendant. The tip sits before the field,
    # so a labelable tip left the field itself unlabelled - eleven of them on the Jobs page alone.
    settings = replace(configured_settings(tmp_path), auto_sync=False)
    app = create_app(settings)

    with TestClient(app) as client:
        pages = {path: client.get(path) for path in ("/jobs", "/imports", "/setup", "/sources")}

    labelable = re.compile(r"<(button|input|select|textarea)\b", re.I)
    for path, page in pages.items():
        assert page.status_code == 200, path
        for block in re.findall(r"<label\b[^>]*>(.*?)</label>", page.text, re.S):
            if "help-tip" not in block:
                continue
            first = labelable.search(block)
            # Whatever the label binds to, it must be the field - never the tip.
            assert first is not None and first.group(1).lower() != "button", f"{path}: {block[:120]}"


def test_the_jobs_page_offers_one_filter_control_that_counts_what_is_set(tmp_path) -> None:
    # On a phone the twelve facet pills are 370px of chrome before the first job, so they
    # collapse behind a single button. It has to say how many filters are actually applied,
    # since collapsed chrome that hides an active filter is worse than visible chrome.
    settings = replace(configured_settings(tmp_path), auto_sync=False)
    app = create_app(settings)

    with TestClient(app) as client:
        unfiltered = client.get("/jobs?show_mismatched_titles=1")
        filtered = client.get("/jobs?show_mismatched_titles=1&work_model=remote&eligibility=eligible")

    for page in (unfiltered, filtered):
        assert 'aria-controls="explorer-facets"' in page.text
        assert 'aria-expanded="false"' in page.text
    toggle = re.search(r'id="filter-toggle".*?</button>', filtered.text, re.S)
    assert toggle is not None and '<span class="facet-count">2</span>' in toggle.group(0)
    assert '<span class="facet-count">' not in re.search(
        r'id="filter-toggle".*?</button>', unfiltered.text, re.S
    ).group(0)


def test_the_companies_page_drops_a_superseded_assessment_and_offers_the_unresearched(tmp_path) -> None:
    # A rules-only profile is only rewritten when someone asks for it, so a summary written by
    # the ruleset that quoted job postings verbatim - mojibake and all - stayed on screen as if
    # it were a current assessment.
    settings = replace(configured_settings(tmp_path), auto_sync=False)
    app = create_app(settings)
    database = app.state.database
    score = {
        "total": 60,
        "dimensions": {
            "domain_alignment": 10, "engineering_environment": 10, "location_mobility": 10,
            "compensation": 10, "company_quality": 10, "evidence_confidence": 10,
        },
        "reasons": [], "risks": [],
    }
    evidence = [{"source_url": "https://example.test/careers", "source_type": "careers", "title": "Careers", "excerpt": "Engineering careers"}]
    database.save_company_research(
        name="Stale Ltd.", domain="example.test",
        profile={"summary": "Senior Software Engineer - Data Platform Who we are â€ the pioneer of", "remote_policy": "unknown", "sponsorship": "unknown", "relocation": "unknown"},
        evidence=evidence, score=score, provider="rules", model="company-rules-v1",
    )
    current_id = database.save_company_research(
        name="Current Ltd.", domain="current.test",
        profile={"summary": "Remote work is described as regional. Visa sponsorship is not stated in the fetched sources.", "remote_policy": "regional", "sponsorship": "unknown", "relocation": "unknown"},
        evidence=evidence, score=score, provider="rules", model=RULES_MODEL,
    )
    database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="unresearched", title="Backend Engineer", company="Unknown Ltd.",
            location="Remote", description="Build backend systems.", url="https://example.test/unresearched",
        )
    )

    with TestClient(app) as client:
        page = client.get("/companies")
        searched = client.get("/companies?q=current")
        detail = client.get(f"/companies/{current_id}")

    # Neither the posting text nor its mojibake reaches the page.
    assert "Senior Software Engineer - Data Platform" not in page.text
    assert "â€" not in page.text
    assert "earlier version of the rules" in page.text
    # The current rules summary says the same three sentences for every company; the facts it
    # extracted are rendered as facts instead.
    assert "not stated in the fetched sources" not in page.text
    assert "sponsorship unknown" in page.text
    # Employers with collected jobs and no assessment are counted and can be researched here.
    assert "1 not researched" in page.text
    assert "Unknown Ltd." in page.text
    assert 'action="/api/companies/research"' in page.text
    # Search narrows both lists.
    assert "Current Ltd." in searched.text and "Stale Ltd." not in searched.text
    assert "Unknown Ltd." not in searched.text
    # A model-written assessment is still shown in full on the profile itself.
    assert detail.status_code == 200
    assert "Remote work is described as regional." in detail.text


def test_duplicate_review_shows_what_differs_and_decides_a_whole_set_at_once(tmp_path) -> None:
    # Three copies of one posting produce three pair rows, and the old table asked about each of
    # them separately while showing the same title, company and location on both sides.
    settings = replace(configured_settings(tmp_path), auto_sync=False)
    app = create_app(settings)
    database = app.state.database
    job_ids = []
    for index, source in enumerate(("greenhouse-example", "linkedin-remote", "arbeitnow")):
        job_id, _ = database.upsert_job(
            CollectedJob(
                source=source, source_job_id=f"copy-{index}", title="Platform Engineer", company="Example",
                location="Remote", description="Build platforms.", url=f"https://{source}.test/jobs/{index}",
            )
        )
        job_ids.append(job_id)

    with TestClient(app) as client:
        page = client.get("/duplicates")
        merged = client.post(
            "/api/duplicates/merge",
            json={"candidate_ids": [1, 2, 3], "keep_job_id": job_ids[1]},
        )
        after = client.get("/duplicates")

    assert page.status_code == 200
    # One set, one heading, and the fields every copy agrees on are stated once.
    assert page.text.count('class="duplicate-set"') == 1
    assert page.text.count("Platform Engineer") == 1
    assert "3 copies" in page.text
    # What distinguishes the copies is on the page: where each came from and where it lives.
    for source in ("greenhouse-example", "linkedin-remote", "arbeitnow"):
        assert source in page.text
    assert "https://arbeitnow.test/jobs/2" in page.text
    # The bulk merge asks before it runs instead of being the page's primary button.
    assert "data-confirm=" in page.text
    # One decision retires every pair in the set, keeping the copy the reader picked.
    assert merged.status_code == 200
    # Three copies, three pair rows, two copies folded into the keeper - one request.
    assert merged.json() == {"job_id": job_ids[1], "merged": 2}
    assert "No duplicates to review" in after.text
    assert [database.get_job(job_id)["merged_into_job_id"] for job_id in job_ids] == [job_ids[1], None, job_ids[1]]


def test_the_score_breakdown_shows_the_shortfall_before_the_numbers(tmp_path) -> None:
    # Six rows of "n / n" made the reader compare six pairs of numbers to find where the points
    # went, and a job with no gaps still spent a whole card saying it had none.
    # Without this the startup sync rescores the job and replaces the evaluation under test.
    app = create_app(replace(configured_settings(tmp_path), auto_sync=False))
    database = app.state.database
    job_id, _ = database.upsert_job(
        CollectedJob(
            source="manual", source_job_id="breakdown", title="Backend Engineer", company="Example",
            location="Remote", description="Build backend systems.", url="https://example.test/breakdown",
        )
    )
    database.save_evaluation(
        job_id,
        EligibilityResult(
            status=EligibilityStatus.ELIGIBLE, route="remote_worldwide", sponsorship="not_required",
            relocation="not_required", location_fit="remote_worldwide", reasons=[], risks=[],
        ),
        ScoreResult(
            total=62,
            dimensions={
                "role_domain": 30, "stack": 20, "domain_experience": 2, "seniority": 5,
                "location_authorization": 20, "salary_employment": 10,
            },
            confidence=0.9, verdict="review", evidence=[], gaps=[], provider="rules", model="test",
        ),
        "scored",
    )

    with TestClient(app) as client:
        page = client.get(f"/jobs/{job_id}")

    factors = re.findall(r'data-score-factor="([^"]+)"', page.text)
    # Seniority lost 10 of 15 and domain experience 8 of 10, so they lead; full factors follow.
    assert factors[:2] == ["seniority", "domain_experience"]
    assert set(factors[2:]) == {"role_domain", "stack", "salary_employment", "location_authorization"}
    # Each factor draws its own fraction rather than only stating it.
    assert 'class="score-factor-bar" style="--fill: 20%"' in page.text
    # Nothing was recorded against this job, so there is no card saying so.
    assert "Gaps and risks" not in page.text
    assert "No material gaps were recorded" in page.text


def test_a_fully_enabled_source_pack_says_so_instead_of_offering_to_enable_it(tmp_path) -> None:
    # "Enable pack" was the primary button on every card, including the eight where every board
    # was already added and switched on, so the most prominent control changed nothing.
    settings = replace(configured_settings(tmp_path), auto_sync=False)
    app = create_app(settings)

    with TestClient(app) as client:
        before = client.get("/sources")
        pack_id = re.search(r'data-source-pack="([^"]+)"', before.text).group(1)
        client.post(f"/api/source-packs/{pack_id}/install", json={"enabled": True})
        after = client.get("/sources")

    def card(page: str) -> str:
        return page.split(f'data-source-pack="{pack_id}"')[1].split("</article>")[0]

    assert 'data-enable="true"' in card(before.text)
    assert "Enabled ✓" not in card(before.text)
    # Nothing left to add or enable: the card reports the state and keeps only the update action.
    assert 'data-enable="true"' not in card(after.text)
    assert "Enabled ✓" in card(after.text)
    assert "Update pack" in card(after.text)
    # The bar duplicated the count line and read as a loading state once it was full.
    assert "source-pack-progress" not in card(after.text)
    # The bare number in the corner now says what it counts.
    assert "</strong> boards" in card(after.text)
