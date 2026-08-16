from __future__ import annotations

import sys

from rolebeacon.cli import main


def test_cli_migrate_copies_database_before_destination_initialization(tmp_path, monkeypatch, capsys) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "data").mkdir(parents=True)
    legacy_database = legacy / "data" / "job-radar.sqlite3"
    legacy_database.write_bytes(b"legacy database fixture")
    destination = tmp_path / "destination"
    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(destination))
    monkeypatch.setenv("ROLEBEACON_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["rolebeacon", "migrate", "--from", str(legacy)])

    main()

    assert (destination / "rolebeacon.sqlite3").read_bytes() == b"legacy database fixture"
    assert '"database": "copied"' in capsys.readouterr().out
