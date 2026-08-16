from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_rolebeacon_environment(tmp_path, monkeypatch) -> None:
    """Keep ambient project .env values and the user's real application data out of tests."""
    for name in tuple(os.environ):
        if name.startswith("ROLEBEACON_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROLEBEACON_ROOT", str(tmp_path))
    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(tmp_path / "data"))
