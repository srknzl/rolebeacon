from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace

import pytest

from rolebeacon import cli
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob
from rolebeacon.setup import SetupService
from rolebeacon.sync import SyncStatus


def _configured_settings(tmp_path, *, mode: str = "rules") -> Settings:
    payload = {
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
        "llm": {"mode": mode, "model": "test-model"},
        "activate": True,
    }
    return SetupService(Settings.load(tmp_path)).complete(payload)


def _seed(database: Database) -> None:
    database.upsert_job(
        CollectedJob(
            source="fixture",
            source_job_id="one",
            title="Backend Engineer",
            company="Example",
            location="Remote",
            description="Build APIs",
            url="https://example.com/jobs/one",
        )
    )


def test_jobs_no_sync_uses_only_local_database_and_prints_paths(tmp_path, monkeypatch, capsys) -> None:
    settings = _configured_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    _seed(database)

    class UnexpectedSync:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("--no-sync must not construct SyncService")

    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(cli, "SyncService", UnexpectedSync)
    monkeypatch.setattr(
        sys,
        "argv",
        ["rolebeacon", "jobs", "--no-sync", "--output-dir", str(tmp_path / "exports")],
    )

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    cli.main()
    human = capsys.readouterr().out

    # Piped, the same command is a machine dump, so a script never has to parse the prose.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    cli.main()
    machine = json.loads(capsys.readouterr().out)

    assert "Sync: skipped" in human
    assert "All jobs: 1" in human
    assert "all-jobs.json" in human
    assert machine["sync"]["phase"] == "skipped"
    assert machine["all_jobs"] == 1
    assert any(path.endswith("all-jobs.json") for path in machine["exports"])
    run_directories = list((tmp_path / "exports").glob("rolebeacon-jobs-*"))
    exported = json.loads((run_directories[0] / "all-jobs.json").read_text(encoding="utf-8"))
    assert exported["sync"] == {"requested": False, "performed": False, "status": None}


def test_jobs_from_json_imports_complete_shared_setup_before_export(tmp_path, monkeypatch) -> None:
    payload = {
        "candidate": {
            "schema_version": "1.0",
            "name": "JSON Candidate",
            "headline": "Distributed Systems Engineer",
            "location": {"country_code": "TR", "country_name": "Türkiye", "city": "İstanbul"},
            "skills": {"Languages": ["Python"]},
        },
        "mobility": {
            "schema_version": "1.0",
            "current_country_code": "TR",
            "work_authorizations": ["TR"],
            "remote_from_current_country": True,
        },
        "preferences": {
            "schema_version": "1.0",
            "target_roles": ["Platform Engineer"],
            "preferred_skills": ["Kubernetes"],
            "daily_review_limit": 12,
        },
        "enabled_source_ids": ["arbeitnow"],
        "llm": {"mode": "rules"},
        "activate": False,
    }
    payload_path = tmp_path / "complete-setup.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "exports"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rolebeacon",
            "jobs",
            "--from-json",
            str(payload_path),
            "--no-sync",
            "--output-dir",
            str(output),
        ],
    )

    cli.main()

    saved = Settings.load()
    assert saved.setup_complete is True
    assert saved.activated is False
    assert saved.load_candidate_profile()["headline"] == "Distributed Systems Engineer"
    assert saved.load_mobility_profile()["remote_from_current_country"] is True
    assert saved.load_search_profile()["target_roles"] == ["Platform Engineer"]
    assert saved.load_search_profile()["preferred_skills"] == ["Kubernetes"]
    assert [source.id for source in saved.load_sources() if source.enabled] == ["arbeitnow"]
    assert len(list(output.glob("rolebeacon-jobs-*"))) == 1


def test_jobs_from_json_requires_activation_before_refresh(tmp_path, monkeypatch) -> None:
    settings = _configured_settings(tmp_path)
    payload = {
        "candidate": settings.load_candidate_profile(),
        "mobility": settings.load_mobility_profile(),
        "preferences": settings.load_search_profile(),
        "enabled_source_ids": [],
        "llm": {"mode": "rules"},
        "activate": False,
    }
    payload_path = tmp_path / "inactive.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "exports"
    monkeypatch.setattr(
        sys,
        "argv",
        ["rolebeacon", "jobs", "--from-json", str(payload_path), "--output-dir", str(output)],
    )

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize(
    ("status", "expected_code", "warning"),
    (
        (SyncStatus(phase="completed_with_errors", source_errors=2), 0, "2 source error(s)"),
        (SyncStatus(phase="failed", error="collector exploded"), 1, "existing local jobs were exported"),
    ),
)
def test_jobs_exports_after_partial_or_fatal_refresh(
    tmp_path, monkeypatch, capsys, status: SyncStatus, expected_code: int, warning: str
) -> None:
    settings = _configured_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    _seed(database)

    class FakeSync:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> SyncStatus:
            return status

    monkeypatch.setattr(cli, "SyncService", FakeSync)
    args = argparse.Namespace(no_sync=False, start_ollama=False, output_dir=tmp_path / "exports")

    assert cli._run_jobs_command(args, settings, database) == expected_code

    captured = capsys.readouterr()
    assert warning in captured.err
    run_directory = next((tmp_path / "exports").glob("rolebeacon-jobs-*"))
    exported = json.loads((run_directory / "all-jobs.json").read_text(encoding="utf-8"))
    assert exported["sync"]["performed"] is True
    assert exported["sync"]["status"]["phase"] == status.phase


def test_jobs_exports_existing_data_after_ollama_start_failure(tmp_path, monkeypatch, capsys) -> None:
    settings = replace(_configured_settings(tmp_path), llm_mode="ollama", llm_enabled=True)
    database = Database(settings.database_path)
    database.initialize()
    _seed(database)

    async def fail_start(_settings) -> dict:
        raise RuntimeError("Ollama executable is unavailable")

    monkeypatch.setattr(cli, "_ensure_ollama_ready", fail_start)
    args = argparse.Namespace(no_sync=False, start_ollama=True, output_dir=tmp_path / "exports")

    assert cli._run_jobs_command(args, settings, database) == 1

    assert "existing local jobs were exported" in capsys.readouterr().err
    run_directory = next((tmp_path / "exports").glob("rolebeacon-jobs-*"))
    exported = json.loads((run_directory / "all-jobs.json").read_text(encoding="utf-8"))
    assert exported["count"] == 1
    assert exported["sync"]["requested"] is True
    assert exported["sync"]["performed"] is False
    assert "Ollama executable is unavailable" in exported["sync"]["status"]["error"]


def test_jobs_exports_existing_data_when_sync_run_raises(tmp_path, capsys, monkeypatch) -> None:
    settings = _configured_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    _seed(database)
    class FailingSyncService:
        def __init__(self, _settings, _database, _llm) -> None:
            pass

        async def run(self) -> dict:
            raise RuntimeError("sync boundary failure")

    monkeypatch.setattr(cli, "SyncService", FailingSyncService)
    args = argparse.Namespace(no_sync=False, start_ollama=False, output_dir=tmp_path / "exports")

    assert cli._run_jobs_command(args, settings, database) == 1

    assert "RuntimeError: sync boundary failure" in capsys.readouterr().err
    run_directory = next((tmp_path / "exports").glob("rolebeacon-jobs-*"))
    exported = json.loads((run_directory / "all-jobs.json").read_text(encoding="utf-8"))
    assert exported["count"] == 1
    assert exported["sync"]["requested"] is True
    assert exported["sync"]["performed"] is True
    assert exported["sync"]["status"]["phase"] == "failed"
    assert exported["sync"]["status"]["error"] == "RuntimeError: sync boundary failure"


def test_jobs_rejects_invalid_start_ollama_invocations(tmp_path, monkeypatch) -> None:
    rules = _configured_settings(tmp_path)
    monkeypatch.setattr(cli.Settings, "load", lambda: rules)

    monkeypatch.setattr(sys, "argv", ["rolebeacon", "jobs", "--start-ollama"])
    with pytest.raises(SystemExit) as wrong_mode:
        cli.main()
    assert wrong_mode.value.code == 2

    ollama = replace(rules, llm_mode="ollama", llm_enabled=True)
    monkeypatch.setattr(cli.Settings, "load", lambda: ollama)
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "jobs", "--no-sync", "--start-ollama"])
    with pytest.raises(SystemExit) as incompatible:
        cli.main()
    assert incompatible.value.code == 2


@pytest.mark.asyncio
async def test_ensure_ollama_ready_skips_start_when_model_is_healthy(tmp_path, monkeypatch) -> None:
    settings = replace(_configured_settings(tmp_path), llm_mode="ollama", llm_enabled=True)

    class HealthyClient:
        def __init__(self, _settings) -> None:
            pass

        async def health(self) -> dict:
            return {"available": True, "status": "available", "error": ""}

    class UnexpectedModels:
        def __init__(self, _settings) -> None:
            raise AssertionError("healthy Ollama must not be started again")

    monkeypatch.setattr(cli, "LlmClient", HealthyClient)
    monkeypatch.setattr(cli, "LocalModelService", UnexpectedModels)

    result = await cli._ensure_ollama_ready(settings)

    assert result["started"] is False


@pytest.mark.asyncio
async def test_ensure_ollama_ready_starts_and_polls_without_pulling(tmp_path, monkeypatch) -> None:
    settings = replace(_configured_settings(tmp_path), llm_mode="ollama", llm_enabled=True)
    health = iter(
        (
            {"available": False, "status": "unavailable", "error": "offline"},
            {"available": False, "status": "unavailable", "error": "starting"},
            {"available": True, "status": "available", "error": ""},
        )
    )
    started: list[str] = []

    class StartingClient:
        def __init__(self, _settings) -> None:
            pass

        async def health(self) -> dict:
            return next(health)

    class Models:
        def __init__(self, _settings) -> None:
            pass

        def start_ollama(self, *, host: str = "") -> dict:
            started.append(host)
            return {"started": True, "pid": 123}

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cli, "LlmClient", StartingClient)
    monkeypatch.setattr(cli, "LocalModelService", Models)
    monkeypatch.setattr(cli.asyncio, "sleep", no_wait)

    result = await cli._ensure_ollama_ready(settings)

    assert started == ["127.0.0.1:11434"]
    assert result["started"] is True
    assert result["health"]["available"] is True


@pytest.mark.asyncio
async def test_ensure_ollama_ready_times_out(tmp_path, monkeypatch) -> None:
    settings = replace(_configured_settings(tmp_path), llm_mode="ollama", llm_enabled=True)

    class OfflineClient:
        def __init__(self, _settings) -> None:
            pass

        async def health(self) -> dict:
            return {"available": False, "status": "unavailable", "error": "model missing"}

    class Models:
        def __init__(self, _settings) -> None:
            pass

        def start_ollama(self, *, host: str = "") -> dict:
            return {"started": True, "pid": 123}

    monkeypatch.setattr(cli, "LlmClient", OfflineClient)
    monkeypatch.setattr(cli, "LocalModelService", Models)

    with pytest.raises(RuntimeError, match="model missing"):
        await cli._ensure_ollama_ready(settings, timeout_seconds=0, poll_interval_seconds=0)


@pytest.mark.asyncio
async def test_ensure_ollama_ready_rejects_lan_endpoint_before_starting(tmp_path, monkeypatch) -> None:
    settings = replace(
        _configured_settings(tmp_path),
        llm_mode="ollama",
        llm_enabled=True,
        llm_base_url="http://desktop.local:11434/v1",
    )

    class UnexpectedClient:
        def __init__(self, _settings) -> None:
            raise AssertionError("LAN endpoint must be rejected before polling or starting Ollama")

    class UnexpectedModels:
        def __init__(self, _settings) -> None:
            raise AssertionError("LAN endpoint must not start local Ollama")

    monkeypatch.setattr(cli, "LlmClient", UnexpectedClient)
    monkeypatch.setattr(cli, "LocalModelService", UnexpectedModels)

    with pytest.raises(RuntimeError, match="loopback endpoint"):
        await cli._ensure_ollama_ready(settings)


@pytest.mark.asyncio
async def test_ensure_ollama_ready_binds_configured_non_default_port(tmp_path, monkeypatch) -> None:
    settings = replace(
        _configured_settings(tmp_path),
        llm_mode="ollama",
        llm_enabled=True,
        llm_base_url="http://127.0.0.1:9999/v1",
    )
    health = iter(
        (
            {"available": False, "status": "unavailable", "error": "offline"},
            {"available": True, "status": "available", "error": ""},
        )
    )
    started: list[str] = []

    class StartingClient:
        def __init__(self, _settings) -> None:
            pass

        async def health(self) -> dict:
            return next(health)

    class Models:
        def __init__(self, _settings) -> None:
            pass

        def start_ollama(self, *, host: str = "") -> dict:
            started.append(host)
            return {"started": True, "pid": 123}

    monkeypatch.setattr(cli, "LlmClient", StartingClient)
    monkeypatch.setattr(cli, "LocalModelService", Models)

    result = await cli._ensure_ollama_ready(settings)

    assert started == ["127.0.0.1:9999"]
    assert result["started"] is True
