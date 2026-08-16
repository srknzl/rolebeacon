from __future__ import annotations

import json
import sys

from rolebeacon.cli import main


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
