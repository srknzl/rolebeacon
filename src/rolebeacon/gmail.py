from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from .config import _write_private_json

GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
GMAIL_LABEL = "Job Alerts"
OAUTH_TRANSACTION_TTL = timedelta(minutes=10)


class GmailCredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_config: dict[str, Any]


class GmailAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_hint: str = ""


class GmailOnboardingService:
    def __init__(self, data_dir: Path, port: int = 8787):
        self.data_dir = data_dir
        self.port = port
        self.credentials_path = data_dir / "gmail-credentials.json"
        self.token_path = data_dir / "gmail-token.json"
        self.pending_path = data_dir / "gmail-oauth-pending.json"
        self.connection_path = data_dir / "gmail-connection.json"

    @staticmethod
    def dependency_available() -> bool:
        try:
            return all(
                importlib.util.find_spec(name) is not None
                for name in ("googleapiclient.discovery", "google_auth_oauthlib.flow", "google.oauth2.credentials")
            )
        except ModuleNotFoundError:
            return False

    def status(self) -> dict[str, Any]:
        connection = self._read_json(self.connection_path)
        authorized = self.token_path.exists()
        label_found = bool(connection.get("label_found", False)) if authorized else False
        reauthorization_required = bool(connection.get("reauthorization_required", False))
        return {
            "dependency_available": self.dependency_available(),
            "credentials_configured": self.credentials_path.exists(),
            "authorized": authorized,
            "account_email": str(connection.get("account_email", "")) if authorized else "",
            "label": GMAIL_LABEL,
            "label_found": label_found,
            "last_verified_at": str(connection.get("last_verified_at", "")),
            "reauthorization_required": reauthorization_required,
            "ready": authorized and label_found and not reauthorization_required,
        }

    def save_client_config(self, value: dict[str, Any]) -> dict[str, Any]:
        installed = value.get("installed")
        if not isinstance(installed, dict) or "web" in value:
            raise ValueError("Choose a Google OAuth client of type Desktop app")
        client_id = str(installed.get("client_id", "")).strip()
        client_secret = str(installed.get("client_secret", "")).strip()
        auth_uri = str(installed.get("auth_uri", "")).strip()
        token_uri = str(installed.get("token_uri", "")).strip()
        redirect_uris = installed.get("redirect_uris", [])
        if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
            raise ValueError("The Desktop OAuth client JSON is missing its client ID or client secret")
        if urlsplit(auth_uri).scheme != "https" or urlsplit(auth_uri).hostname != "accounts.google.com":
            raise ValueError("The Desktop OAuth client has an unexpected authorization endpoint")
        if urlsplit(token_uri).scheme != "https" or urlsplit(token_uri).hostname != "oauth2.googleapis.com":
            raise ValueError("The Desktop OAuth client has an unexpected token endpoint")
        if not isinstance(redirect_uris, list) or not any(
            urlsplit(str(uri)).hostname in {"127.0.0.1", "localhost", "::1"} for uri in redirect_uris
        ):
            raise ValueError("The Desktop OAuth client must allow a loopback redirect")
        existing_id = str(self._read_json(self.credentials_path).get("installed", {}).get("client_id", ""))
        _write_private_json(self.credentials_path, value)
        if existing_id and existing_id != client_id:
            self.token_path.unlink(missing_ok=True)
            self.connection_path.unlink(missing_ok=True)
        self.pending_path.unlink(missing_ok=True)
        return self.status()

    def authorization_url(self, *, login_hint: str = "") -> str:
        self._require_dependencies()
        client_config = self._read_json(self.credentials_path)
        if not client_config:
            raise RuntimeError("Import a Google Desktop OAuth client before connecting Gmail")
        from google_auth_oauthlib.flow import Flow

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        redirect_uri = f"http://127.0.0.1:{self.port}/api/setup/gmail/callback"
        flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, state=state)
        flow.redirect_uri = redirect_uri
        authorization_url, returned_state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            code_challenge=challenge,
            code_challenge_method="S256",
            login_hint=login_hint or None,
        )
        _write_private_json(
            self.pending_path,
            {
                "state": returned_state,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        return authorization_url

    def complete_authorization(self, *, state: str, code: str) -> dict[str, Any]:
        self._require_dependencies()
        pending = self._read_json(self.pending_path)
        if not pending or not secrets.compare_digest(str(pending.get("state", "")), state):
            raise ValueError("The Gmail authorization state is missing or invalid")
        try:
            created_at = datetime.fromisoformat(str(pending.get("created_at", "")))
        except ValueError as error:
            raise ValueError("The Gmail authorization state is invalid") from error
        if created_at.tzinfo is None or datetime.now(UTC) - created_at.astimezone(UTC) > OAUTH_TRANSACTION_TTL:
            self.pending_path.unlink(missing_ok=True)
            raise ValueError("The Gmail authorization request expired; start it again from Settings")
        if not code:
            raise ValueError("Google did not return an authorization code")

        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(self._read_json(self.credentials_path), scopes=GMAIL_SCOPES, state=state)
        flow.redirect_uri = str(pending["redirect_uri"])
        flow.code_verifier = str(pending["code_verifier"])
        try:
            flow.fetch_token(code=code)
            _write_private_json(self.token_path, json.loads(flow.credentials.to_json()))
            _write_private_json(
                self.connection_path,
                {
                    "account_email": "",
                    "label_found": False,
                    "last_verified_at": "",
                    "reauthorization_required": False,
                },
            )
        finally:
            self.pending_path.unlink(missing_ok=True)
        return self.status()

    def test_connection(self) -> dict[str, Any]:
        try:
            service = self.authorized_service()
            profile = service.users().getProfile(userId="me").execute()
            labels = service.users().labels().list(userId="me").execute().get("labels", [])
            label_found = any(str(label.get("name", "")) == GMAIL_LABEL for label in labels)
            _write_private_json(
                self.connection_path,
                {
                    "account_email": str(profile.get("emailAddress", "")),
                    "label_found": label_found,
                    "last_verified_at": datetime.now(UTC).isoformat(),
                    "reauthorization_required": False,
                },
            )
        except Exception as error:
            if self.token_path.exists():
                connection = self._read_json(self.connection_path)
                _write_private_json(
                    self.connection_path,
                    {
                        **connection,
                        "reauthorization_required": self._is_authentication_error(error),
                        "last_verified_at": datetime.now(UTC).isoformat(),
                    },
                )
            raise RuntimeError(f"Could not verify Gmail: {type(error).__name__}: {error}") from error
        return self.status()

    def authorized_service(self) -> Any:
        self._require_dependencies()
        if not self.token_path.exists():
            raise RuntimeError("Connect Gmail in RoleBeacon Settings before refreshing LinkedIn alerts")
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials.from_authorized_user_file(self.token_path, GMAIL_SCOPES)
        try:
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                _write_private_json(self.token_path, json.loads(credentials.to_json()))
        except RefreshError as error:
            raise RuntimeError("Gmail authorization expired or was revoked; reconnect it in Settings") from error
        if not credentials.valid:
            raise RuntimeError("Gmail authorization is not valid; reconnect it in Settings")
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def _require_dependencies(self) -> None:
        if not self.dependency_available():
            raise RuntimeError("Install RoleBeacon with the gmail extra to enable Gmail alerts")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _is_authentication_error(error: Exception) -> bool:
        return type(error).__name__ in {"RefreshError", "TokenExpiredError"} or "invalid_grant" in str(error)
