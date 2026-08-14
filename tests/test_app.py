from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from time import sleep

import pytest
from fastapi.testclient import TestClient

from rolebeacon.app import create_app
from rolebeacon.config import Settings
from rolebeacon.domain import CollectedJob, EligibilityResult, EligibilityStatus
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


def test_manual_refresh_is_allowed_when_only_scheduled_collection_is_disabled(tmp_path, monkeypatch) -> None:
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

    assert response.status_code == 202
    assert calls == [(False, True)]


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
            "domain_experience": 15,
            "seniority": 8,
            "location_authorization": 12,
            "salary_employment": 5,
        },
    }
    value["confidence"] = 80
    LlmClient._normalize_score(value, EligibilityStatus.ELIGIBLE)
    LlmClient._validate_score(value)

    assert value["total"] == 75
    assert value["confidence"] == 0.8
    assert value["verdict"] == "review"


def test_llm_rubric_uses_full_point_ranges_and_positive_evidence() -> None:
    assert "not 0-to-1 ratings" in SCORING_RUBRIC
    assert "role_domain (0-25)" in SCORING_RUBRIC
    assert 'never write "absent"' in SCORING_RUBRIC.casefold()


def test_llm_semantic_validation_rejects_negative_evidence_and_generic_gaps() -> None:
    value = {
        "dimensions": {
            "role_domain": 0,
            "stack": 0,
            "domain_experience": 10,
            "seniority": 8,
            "location_authorization": 15,
            "salary_employment": 5,
        },
        "evidence": [
            {
                "requirement": "stack",
                "profile_evidence": "Candidate knows Java, but the role requires React.",
            }
        ],
        "gaps": [
            {"requirement": "stack", "severity": "high"},
            {"requirement": "stack", "severity": "high"},
        ],
    }

    with pytest.raises(ValueError, match="zero-score dimension stack"):
        LlmClient._validate_score_semantics(value)


@pytest.mark.asyncio
async def test_llm_score_retries_with_specific_semantic_feedback(tmp_path) -> None:
    invalid = {
        "dimensions": {
            "role_domain": 0, "stack": 0, "domain_experience": 0,
            "seniority": 0, "location_authorization": 15, "salary_employment": 5,
        },
        "confidence": 0.2,
        "evidence": [{"requirement": "stack", "profile_evidence": "Java, but the role needs React"}],
        "gaps": [{"requirement": "stack", "severity": "high"}],
    }
    corrected = {
        **invalid,
        "evidence": [
            {
                "requirement": "location_authorization",
                "profile_evidence": "Worldwide remote work explicitly includes the candidate location.",
            }
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
        {"location": {"country_code": "TR", "country_name": "Türkiye"}},
    )

    assert score.gaps == [{"requirement": "React", "severity": "high"}]
    assert len(client.calls) == 2
    assert "previous JSON is invalid" in client.calls[1][-1]["content"]


def test_qwen3_prompts_disable_thinking_for_structured_output(tmp_path) -> None:
    settings = SetupService(Settings.load(tmp_path)).complete(setup_payload())
    settings = replace(settings, llm_model="qwen3:14b")

    assert LlmClient(settings)._prompt_for_model("Return JSON").endswith("/no_think")


def test_ollama_native_payload_disables_thinking_and_uses_json_schema(tmp_path) -> None:
    settings = replace(Settings.load(tmp_path), llm_mode="ollama", llm_enabled=True, llm_model="qwen3:14b")
    schema = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}

    payload = LlmClient(settings)._ollama_payload(
        [{"role": "user", "content": "Return JSON"}], schema, temperature=0.1, max_tokens=900
    )

    assert payload["think"] is False
    assert payload["format"] == schema
    assert payload["options"]["num_predict"] == 900


def test_setup_shows_searchable_country_catalog_and_rules_model_status(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        setup = client.get("/setup")
        model_status = client.get("/api/model/status")

    assert setup.status_code == 200
    assert 'data-country-code="TR" data-country-name="Türkiye"' in setup.text
    assert 'data-country-code="DE" data-country-name="Germany"' in setup.text
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
        feedback = client.post(f"/api/jobs/{job_id}/feedback", json={"status": "interested"})

    assert dashboard.status_code == 200
    assert "RoleBeacon" in dashboard.text
    assert "No recommended jobs yet" in dashboard.text
    assert jobs.json()["jobs"][0]["title"] == "Backend Engineer"
    assert feedback.json()["status"] == "interested"
    assert database.get_job(job_id)["status"] == "interested"


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
    assert "These fields do not influence job discovery" in page.text
    assert 'id="model-details"' in page.text
    assert 'modelDetails.hidden = document.getElementById("llm-mode").value === "rules"' in page.text
    assert 'message("Preferences saved.", true)' in page.text
    assert "Arbeitnow roles that explicitly advertise visa sponsorship" in page.text
    assert "Searches every relocation-target country or continent you've added" in page.text


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
        decision = client.post(f"/api/jobs/{job_id}/feedback", json={"status": "interested"})

    assert page.status_code == 200
    assert "data-decision-form" in page.text
    assert "event.preventDefault()" in page.text
    assert "The tailored résumé uses only your locally stored candidate profile" in page.text
    assert "Cover letter requires an LLM" in page.text
    assert decision.status_code == 200
    assert app.state.database.get_job(job_id)["status"] == "interested"


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
