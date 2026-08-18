from __future__ import annotations

import json

import pytest

from rolebeacon.config import Settings


def test_atomic_json_write_preserves_previous_generation_when_replace_fails(tmp_path, monkeypatch) -> None:
    settings = Settings.load(tmp_path)
    settings.ensure_directories()
    settings.save_search_profile({"schema_version": "1.0", "target_roles": ["Backend Engineer"]})

    def fail_replace(_source, _destination):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("rolebeacon.config.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        settings.save_search_profile({"schema_version": "1.0", "target_roles": ["Corrupt"]})

    assert json.loads(settings.search_profile_path.read_text())["target_roles"] == ["Backend Engineer"]


def test_merge_default_sources_drops_instances_with_a_retired_collector_kind(tmp_path) -> None:
    # A source instance whose collector was removed (e.g. a retired feature) can never sync again;
    # ensure_directories() must prune it instead of leaving a permanently-broken entry behind.
    settings = Settings.load(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.source_config_path.write_text(
        json.dumps(
            [
                {"id": "orphaned-source", "kind": "no_longer_supported", "name": "Orphan", "enabled": True},
            ]
        ),
        encoding="utf-8",
    )

    settings.ensure_directories()

    persisted = json.loads(settings.source_config_path.read_text())
    assert all(item["id"] != "orphaned-source" for item in persisted)


def test_save_search_profile_updates_the_manifest_source_of_truth(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    settings.ensure_directories()
    settings.setup_state_path.write_text(
        json.dumps(
            {
                "completed": True,
                "configuration": {
                    "candidate": {"schema_version": "1.0", "name": "Candidate"},
                    "mobility": {"schema_version": "1.0", "current_country_code": "TR"},
                    "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
                    "strategies": [],
                },
            }
        ),
        encoding="utf-8",
    )

    destination = settings.save_search_profile(
        {"schema_version": "1.0", "target_roles": ["Platform Engineer"]}
    )

    assert destination == settings.setup_state_path
    assert settings.load_search_profile()["target_roles"] == ["Platform Engineer"]


def _save_setup(settings: Settings, *, roles: list[str], activate: bool = True) -> Settings:
    return settings.save_setup(
        candidate={"schema_version": "1.0", "name": "Candidate"},
        mobility={"schema_version": "1.0", "current_country_code": "TR"},
        preferences={"schema_version": "1.0", "target_roles": roles},
        strategies=[],
        enabled_source_ids=[],
        llm={"mode": "rules"},
        activate=activate,
    )


def test_save_setup_preserves_sibling_keys_it_does_not_own(tmp_path) -> None:
    # save_setup used to rebuild setup.json from a literal, so the Prepare application toggle
    # was silently reset every time an unrelated Settings save ran.
    settings = Settings.load(tmp_path)
    settings.ensure_directories()
    _save_setup(settings, roles=["Backend Engineer"])
    settings.save_open_browser(True)

    _save_setup(settings, roles=["Platform Engineer"])
    stored = json.loads(settings.setup_state_path.read_text(encoding="utf-8"))

    assert stored["open_browser"] is True
    assert Settings.load(tmp_path).open_browser is True


def test_save_setup_replaces_the_configuration_document_wholesale(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    settings.ensure_directories()
    _save_setup(settings, roles=["Backend Engineer"])
    first = json.loads(settings.setup_state_path.read_text(encoding="utf-8"))

    _save_setup(settings, roles=["Platform Engineer"], activate=False)
    second = json.loads(settings.setup_state_path.read_text(encoding="utf-8"))

    assert second["configuration"]["preferences"]["target_roles"] == ["Platform Engineer"]
    assert second["activated"] is False
    assert second["generation"] != first["generation"]


def test_private_json_write_is_portable_when_fchmod_is_unavailable(tmp_path, monkeypatch) -> None:
    settings = Settings.load(tmp_path)
    monkeypatch.delattr("rolebeacon.config.os.fchmod", raising=False)

    settings.save_search_profile({"schema_version": "1.0", "target_roles": ["Backend Engineer"]})

    assert settings.load_search_profile()["target_roles"] == ["Backend Engineer"]
