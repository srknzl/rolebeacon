from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .config import Settings

LEGACY_ENV_MAP = {
    "JOB_RADAR_HOST": "ROLEBEACON_HOST",
    "JOB_RADAR_PORT": "ROLEBEACON_PORT",
    "JOB_RADAR_AUTO_SYNC": "ROLEBEACON_AUTO_SYNC",
    "JOB_RADAR_OPEN_BROWSER": "ROLEBEACON_OPEN_BROWSER",
    "JOB_RADAR_SYNC_INTERVAL_SECONDS": "ROLEBEACON_SYNC_INTERVAL_SECONDS",
    "JOB_RADAR_OVERLAP_HOURS": "ROLEBEACON_OVERLAP_HOURS",
    "JOB_RADAR_INITIAL_LOOKBACK_DAYS": "ROLEBEACON_INITIAL_LOOKBACK_DAYS",
}


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def import_legacy(settings: Settings, legacy_root: Path) -> dict[str, Any]:
    legacy_root = legacy_root.expanduser().resolve()
    if not legacy_root.is_dir():
        raise FileNotFoundError(f"Legacy directory does not exist: {legacy_root}")
    settings.ensure_directories()
    manifest_path = settings.data_dir / "legacy-import.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    result: dict[str, Any] = {
        "source": str(legacy_root),
        "database": "not_found",
        "applications_copied": 0,
        "candidate_profile": "not_found",
        "environment": {},
        "credentials_imported": False,
    }

    database_candidates = [legacy_root / "data" / "job-radar.sqlite3", legacy_root / "data" / "rolebeacon.sqlite3"]
    legacy_database = next((path for path in database_candidates if path.exists()), None)
    if legacy_database:
        digest = _digest(legacy_database)
        if previous.get("database_sha256") == digest and settings.database_path.exists():
            result["database"] = "already_imported"
        elif settings.database_path.exists():
            result["database"] = "skipped_destination_exists"
        else:
            shutil.copy2(legacy_database, settings.database_path)
            result["database"] = "copied"
        result["database_sha256"] = digest

    legacy_applications = legacy_root / "data" / "applications"
    if legacy_applications.is_dir():
        destination = settings.data_dir / "applications"
        for source in legacy_applications.iterdir():
            if not source.is_dir():
                continue
            target = destination / source.name
            if not target.exists():
                shutil.copytree(source, target)
                result["applications_copied"] += 1

    legacy_env = _dotenv(legacy_root / ".env")
    mapped = {new: legacy_env[old] for old, new in LEGACY_ENV_MAP.items() if old in legacy_env}
    if mapped:
        migrated_path = settings.data_dir / "legacy-environment.json"
        migrated_path.write_text(json.dumps(mapped, indent=2) + "\n", encoding="utf-8")
        result["environment"] = mapped

    candidate_value = legacy_env.get("JOB_RADAR_CANDIDATE_PROFILE", "")
    candidate_path = Path(candidate_value).expanduser() if candidate_value else legacy_root / "candidate-profile.json"
    if candidate_path.exists():
        target = settings.data_dir / "legacy-candidate-profile.json"
        if not target.exists():
            shutil.copy2(candidate_path, target)
            result["candidate_profile"] = "copied_for_review"
        else:
            result["candidate_profile"] = "already_imported"

    # OAuth tokens and persistent browser sessions intentionally require fresh authorization.
    manifest_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result
