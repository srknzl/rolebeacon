from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rolebeacon.app import create_app
from rolebeacon.config import Settings
from rolebeacon.gmail import GMAIL_LABEL, GmailOnboardingService

# Rules-only mode is a complete product mode, so the documented `uv sync --extra dev` workflow must
# stay green without the optional Gmail packages. CI installs the extra so these still run there.
requires_gmail_extra = pytest.mark.skipif(
    not GmailOnboardingService.dependency_available(),
    reason="requires the optional gmail extra",
)


def desktop_client() -> dict:
    return {
        "installed": {
            "client_id": "client.apps.googleusercontent.com",
            "project_id": "rolebeacon-local",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": "client-secret",
            "redirect_uris": ["http://localhost"],
        }
    }


def test_client_config_is_validated_and_stored_privately(tmp_path) -> None:
    service = GmailOnboardingService(tmp_path)

    status = service.save_client_config(desktop_client())

    assert status["credentials_configured"] is True
    assert "client-secret" not in json.dumps(status)
    assert stat.S_IMODE(service.credentials_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


@pytest.mark.parametrize(
    "value, message",
    [
        ({"web": desktop_client()["installed"]}, "Desktop app"),
        (
            {"installed": {**desktop_client()["installed"], "token_uri": "https://example.test/token"}},
            "token endpoint",
        ),
        (
            {"installed": {**desktop_client()["installed"], "redirect_uris": ["https://example.test/callback"]}},
            "loopback redirect",
        ),
    ],
)
def test_client_config_rejects_wrong_oauth_client_shapes(tmp_path, value, message) -> None:
    with pytest.raises(ValueError, match=message):
        GmailOnboardingService(tmp_path).save_client_config(value)


@requires_gmail_extra
def test_authorization_url_uses_loopback_pkce_and_private_pending_state(tmp_path) -> None:
    service = GmailOnboardingService(tmp_path, port=9876)
    service.save_client_config(desktop_client())

    url = service.authorization_url(login_hint="candidate@example.com")
    pending = json.loads(service.pending_path.read_text(encoding="utf-8"))

    assert url.startswith("https://accounts.google.com/")
    assert "code_challenge_method=S256" in url
    assert "login_hint=candidate%40example.com" in url
    assert pending["redirect_uri"] == "http://127.0.0.1:9876/api/setup/gmail/callback"
    assert pending["state"] in url
    assert stat.S_IMODE(service.pending_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


@requires_gmail_extra
def test_authorization_callback_rejects_expired_or_replayed_state(tmp_path) -> None:
    service = GmailOnboardingService(tmp_path)
    service.save_client_config(desktop_client())
    service.authorization_url()
    pending = json.loads(service.pending_path.read_text(encoding="utf-8"))
    pending["created_at"] = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    service.pending_path.write_text(json.dumps(pending), encoding="utf-8")

    with pytest.raises(ValueError, match="expired"):
        service.complete_authorization(state=pending["state"], code="code")
    with pytest.raises(ValueError, match="missing or invalid"):
        service.complete_authorization(state=pending["state"], code="code")


def test_collector_authorization_never_starts_an_interactive_flow(tmp_path, monkeypatch) -> None:
    service = GmailOnboardingService(tmp_path)
    monkeypatch.setattr(service, "dependency_available", lambda: True)

    with pytest.raises(RuntimeError, match="Connect Gmail in RoleBeacon Settings"):
        service.authorized_service()


def test_connection_verifies_exact_label_and_records_no_message_content(tmp_path, monkeypatch) -> None:
    service = GmailOnboardingService(tmp_path)
    service.token_path.write_text("{}", encoding="utf-8")

    class Executable:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Users:
        def getProfile(self, **_kwargs):
            return Executable({"emailAddress": "candidate@example.com"})

        def labels(self):
            return self

        def list(self, **_kwargs):
            return Executable({"labels": [{"id": "Label_1", "name": GMAIL_LABEL}]})

    class Gmail:
        def users(self):
            return Users()

    monkeypatch.setattr(service, "authorized_service", lambda: Gmail())

    status = service.test_connection()

    assert status["ready"] is True
    assert status["account_email"] == "candidate@example.com"
    stored = service.connection_path.read_text(encoding="utf-8")
    assert "candidate@example.com" in stored
    assert "message" not in stored


def test_setup_page_and_gmail_api_expose_readiness_without_secrets(tmp_path, monkeypatch) -> None:
    app = create_app(Settings.load(tmp_path))
    service = app.state.gmail_onboarding
    monkeypatch.setattr(service, "dependency_available", lambda: True)
    monkeypatch.setattr(service, "authorization_url", lambda **_kwargs: "https://accounts.google.test/authorize")
    monkeypatch.setattr(
        service,
        "test_connection",
        lambda: {
            **service.status(),
            "authorized": True,
            "account_email": "candidate@example.com",
            "label_found": True,
            "ready": True,
        },
    )

    with TestClient(app) as client:
        page = client.get("/setup")
        settings_page = client.get("/settings")
        saved = client.post("/api/setup/gmail/credentials", json={"client_config": desktop_client()})
        authorization = client.post(
            "/api/setup/gmail/authorize", json={"login_hint": "candidate@example.com"}
        )
        tested = client.post("/api/setup/gmail/test")

    assert page.status_code == 200
    assert 'id="setup-wizard"' in page.text
    assert "Fill the important fields yourself" in page.text
    assert "Ask an LLM for setup JSON" in page.text
    assert "Curated source packs" in page.text
    assert "Developer infrastructure" in page.text
    assert 'class="source-pack-choice"' in page.text
    assert "LinkedIn Job Alerts through Gmail" in page.text
    assert 'id="linkedin-alerts-source"' in page.text
    assert "client-secret" not in page.text
    assert settings_page.status_code == 200
    assert 'id="setup-wizard"' not in settings_page.text
    assert "Edit your RoleBeacon preferences" in settings_page.text
    assert saved.status_code == 200
    assert "client-secret" not in saved.text
    assert authorization.json() == {"authorization_url": "https://accounts.google.test/authorize"}
    assert tested.json()["ready"] is True


def test_hidden_wizard_steps_cannot_leak_the_activate_and_save_bar(tmp_path) -> None:
    # .setup-actions sets display: flex, which outranks the hidden attribute the wizard uses to
    # switch steps. Without an explicit opt-out the final activate-and-save bar stays on screen
    # for all seven steps, and pressing it there dies in native validation of hidden required
    # inputs without ever reaching the submit handler.
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        page = client.get("/setup")
        stylesheet = client.get("/static/style.css")

    assert 'class="setup-actions wizard-panel" data-wizard-step="review"' in page.text
    assert ".wizard-panel[hidden] { display: none; }" in stylesheet.text


def test_gmail_credentials_api_rejects_web_clients_without_writing(tmp_path) -> None:
    app = create_app(Settings.load(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/setup/gmail/credentials",
            json={"client_config": {"web": desktop_client()["installed"]}},
        )

    assert response.status_code == 422
    assert not app.state.gmail_onboarding.credentials_path.exists()
