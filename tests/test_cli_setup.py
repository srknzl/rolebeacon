from __future__ import annotations

import io
import json
import sys

import pytest
from fastapi.testclient import TestClient

from rolebeacon.cli import main
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.setup import LocalModelService, SetupService
from rolebeacon.sync import SyncService, SyncStatus


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


def test_cli_setup_drives_the_wizard_through_real_terminal_input(tmp_path, monkeypatch, capsys) -> None:
    """Cover the entry point the scripted wizard tests bypass: a default Terminal on real stdin."""

    class Console(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ROLEBEACON_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", Console("q\n"))
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "setup"])

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 1
    output = capsys.readouterr().out
    assert "Step 1 of 6 — Start" in output
    assert "Setup cancelled. Nothing was saved." in output
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


def _setup_without_syncing(tmp_path, monkeypatch) -> Settings:
    """A completed profile whose sync does nothing, so the CLI wiring can run without a network."""
    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ROLEBEACON_ROOT", str(tmp_path))

    async def no_sync(self, force: bool = False, manual: bool = False) -> SyncStatus:
        return SyncStatus()

    monkeypatch.setattr(SyncService, "run", no_sync)
    return SetupService(Settings.load()).complete({**_payload(), "activate": True})


def test_interactive_sync_states_the_risk_before_it_opens_a_window(tmp_path, monkeypatch, capsys) -> None:
    settings = _setup_without_syncing(tmp_path, monkeypatch)
    signed_in = next(source for source in settings.load_sources() if source.kind == "linkedin_browser")
    settings.set_source_enabled(signed_in.id, True)
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "sync", "--interactive"])

    main()

    warning = capsys.readouterr().err
    assert "against LinkedIn's User Agreement" in warning
    assert signed_in.name in warning
    assert "Ctrl-C" in warning


def test_interactive_sync_says_so_when_no_signed_in_source_is_enabled(tmp_path, monkeypatch, capsys) -> None:
    _setup_without_syncing(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "sync", "--interactive"])

    main()

    assert "--interactive has no effect" in capsys.readouterr().err


def test_status_summarizes_for_a_person_and_still_dumps_json_for_a_script(tmp_path, monkeypatch, capsys) -> None:
    # `status` printed 5,140 lines of source rows, so "is anything broken?" needed jq to answer.
    settings = _setup_without_syncing(tmp_path, monkeypatch)
    healthy, broken = settings.load_sources()[:2]
    for source in (healthy, broken):
        settings.set_source_enabled(source.id, True)
    database = Database(settings.database_path)
    database.initialize()
    database.start_source(healthy.id)
    database.finish_source(healthy.id, seen=633, changed=0)
    database.start_source(broken.id)
    database.fail_source(broken.id, "connection refused")

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "status"])
    main()
    human = capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["rolebeacon", "status", "--json"])
    main()
    machine = json.loads(capsys.readouterr().out)

    assert len(human.splitlines()) < 12
    assert "active jobs" in human
    assert "633 seen" in human
    assert "Needs attention:" in human
    assert f"{broken.name}" in human and "connection refused" in human
    # Only the source that actually failed; the one that ran cleanly is not listed.
    attention = [line.strip() for line in human.split("Needs attention:")[1].splitlines() if line.strip()]
    assert attention == [f"{broken.name}  error: connection refused"]
    # The dump a script parses is unchanged, and reachable off a terminal.
    assert set(machine) == {"stats", "sources"}
    assert machine["stats"]["total"] == 0


def test_status_still_prints_json_when_stdout_is_not_a_terminal(tmp_path, monkeypatch, capsys) -> None:
    _setup_without_syncing(tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "status"])

    main()

    assert set(json.loads(capsys.readouterr().out)) == {"stats", "sources"}
