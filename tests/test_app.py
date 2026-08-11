from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from rolebeacon.app import create_app
from rolebeacon.config import Settings
from rolebeacon.domain import CollectedJob
from rolebeacon.llm import LlmClient
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
    assert jobs.json()["jobs"][0]["title"] == "Backend Engineer"
    assert feedback.json()["status"] == "interested"
    assert database.get_job(job_id)["status"] == "interested"


def test_invalid_feedback_is_rejected(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/jobs/1/feedback", json={"status": "submitted_by_bot"})

    assert response.status_code == 422


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
