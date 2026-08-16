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
