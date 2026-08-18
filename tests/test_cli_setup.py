from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

from rolebeacon.cli import main
from rolebeacon.config import Settings
from rolebeacon.setup import LocalModelService, SetupService


def _payload() -> dict:
    return {
        "candidate": {
            "schema_version": "1.0",
            "name": "CLI Candidate",
            "location": {"country_code": "TR", "country_name": "Türkiye"},
        },
        "mobility": {
            "schema_version": "1.0",
            "current_country_code": "TR",
            "work_authorizations": ["TR"],
        },
        "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
        "enabled_source_ids": [],
        "llm": {"mode": "rules"},
        "activate": True,
    }


def test_cli_setup_import_uses_shared_schema_and_requires_explicit_activation(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_path = tmp_path / "setup.json"
    payload_path.write_text(json.dumps(_payload()), encoding="utf-8")
    destination = tmp_path / "data"
    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(destination))
    monkeypatch.setenv("ROLEBEACON_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "setup", "--from-json", str(payload_path)])

    main()

    result = json.loads(capsys.readouterr().out)
    assert result["setup_complete"] is True
    assert result["activated"] is False
    assert json.loads((destination / "setup.json").read_text(encoding="utf-8"))["activated"] is False


def test_cli_setup_import_reports_schema_errors_instead_of_raising(tmp_path, monkeypatch) -> None:
    payload_path = tmp_path / "setup.json"
    payload_path.write_text(json.dumps({**_payload(), "preferences": {"schema_version": "1.0"}}), encoding="utf-8")
    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ROLEBEACON_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "setup", "--from-json", str(payload_path)])

    with pytest.raises(SystemExit) as error:
        main()

    assert "target_roles" in str(error.value)
    assert not Settings.load().setup_complete


def test_cli_setup_refuses_to_start_the_wizard_without_a_terminal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ROLEBEACON_ROOT", str(tmp_path))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "setup"])

    with pytest.raises(SystemExit):
        main()

    monkeypatch.setattr(sys, "argv", ["rolebeacon", "setup", "--no-interactive"])
    with pytest.raises(SystemExit):
        main()

    assert not Settings.load().setup_complete


def test_cli_port_override_updates_the_app_origin_allowlist(tmp_path, monkeypatch) -> None:
    payload = _payload()
    payload["activate"] = False
    settings = SetupService(Settings.load(tmp_path)).complete(payload)
    observed: dict[str, object] = {}

    def run(app, *, host: str, port: int) -> None:
        observed.update(host=host, port=port, configured_port=app.state.settings.port)
        with TestClient(app, base_url=f"http://{host}:{port}") as client:
            observed["status_code"] = client.get("/").status_code

    monkeypatch.setattr("rolebeacon.cli.Settings.load", lambda: settings)
    monkeypatch.setattr("rolebeacon.cli.uvicorn.run", run)
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "serve", "--port", "9911"])

    main()

    assert observed == {"host": "127.0.0.1", "port": 9911, "configured_port": 9911, "status_code": 200}


def test_start_ollama_sets_requested_bind_host(tmp_path, monkeypatch) -> None:
    settings = Settings.load(tmp_path)
    observed: dict[str, object] = {}

    class Process:
        pid = 123

    def popen(arguments, **options):
        observed.update(arguments=arguments, options=options)
        return Process()

    monkeypatch.setattr("rolebeacon.setup.shutil.which", lambda _name: "/usr/local/bin/ollama")
    monkeypatch.setattr("rolebeacon.setup.subprocess.Popen", popen)
    monkeypatch.setenv("ROLEBEACON_TEST_ENV", "preserved")

    result = LocalModelService(settings).start_ollama(host="127.0.0.1:9999")

    assert observed["arguments"] == ["/usr/local/bin/ollama", "serve"]
    environment = observed["options"]["env"]
    assert environment["OLLAMA_HOST"] == "127.0.0.1:9999"
    assert environment["ROLEBEACON_TEST_ENV"] == "preserved"
    assert result["pid"] == 123
