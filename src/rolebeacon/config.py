from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from platformdirs import user_data_path

from .domain import SourceConfig


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows ACLs do not map directly to POSIX mode bits.
        pass


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    resource_dir: Path
    data_dir: Path
    database_path: Path
    source_config_path: Path
    candidate_profile_path: Path
    mobility_profile_path: Path
    search_profile_path: Path
    strategies_path: Path
    setup_state_path: Path
    secrets_path: Path
    host: str
    port: int
    auto_sync: bool
    open_browser: bool
    sync_interval_seconds: int
    overlap_hours: int
    initial_lookback_days: int
    setup_complete: bool
    activated: bool
    llm_mode: str
    llm_enabled: bool
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout_seconds: float
    resume_renderer: str
    external_resume_command: tuple[str, ...]

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        package_dir = Path(__file__).resolve().parent
        project_root = (root or Path(os.getenv("ROLEBEACON_ROOT", Path.cwd()))).resolve()
        _load_dotenv(project_root / ".env")
        if root is not None:
            default_data_dir = project_root / "data"
        else:
            default_data_dir = Path(user_data_path("RoleBeacon", "RoleBeacon", ensure_exists=False))
        data_dir = Path(os.getenv("ROLEBEACON_DATA_DIR", default_data_dir)).expanduser().resolve()
        setup_state_path = data_dir / "setup.json"
        secrets_path = data_dir / "secrets.json"
        setup = _read_json(setup_state_path, {})
        secrets = _read_json(secrets_path, {})
        llm = setup.get("llm", {}) if isinstance(setup.get("llm"), dict) else {}
        mode = os.getenv("ROLEBEACON_LLM_MODE", str(llm.get("mode", "rules")))
        api_key = os.getenv("ROLEBEACON_LLM_API_KEY", str(secrets.get("llm_api_key", "")))
        return cls(
            root=project_root,
            resource_dir=package_dir / "resources",
            data_dir=data_dir,
            database_path=Path(os.getenv("ROLEBEACON_DB", data_dir / "rolebeacon.sqlite3")).resolve(),
            source_config_path=data_dir / "sources.json",
            candidate_profile_path=data_dir / "candidate-profile.json",
            mobility_profile_path=data_dir / "mobility-profile.json",
            search_profile_path=data_dir / "search-preferences.json",
            strategies_path=data_dir / "search-strategies.json",
            setup_state_path=setup_state_path,
            secrets_path=secrets_path,
            host=os.getenv("ROLEBEACON_HOST", "127.0.0.1"),
            port=int(os.getenv("ROLEBEACON_PORT", "8787")),
            auto_sync=_boolean("ROLEBEACON_AUTO_SYNC", True),
            open_browser=_boolean("ROLEBEACON_OPEN_BROWSER", False),
            sync_interval_seconds=int(os.getenv("ROLEBEACON_SYNC_INTERVAL_SECONDS", str(4 * 60 * 60))),
            overlap_hours=int(os.getenv("ROLEBEACON_OVERLAP_HOURS", "72")),
            initial_lookback_days=int(os.getenv("ROLEBEACON_INITIAL_LOOKBACK_DAYS", "30")),
            setup_complete=bool(setup.get("completed", False)),
            activated=bool(setup.get("activated", False)),
            llm_mode=mode,
            llm_enabled=mode in {"ollama", "custom"},
            llm_base_url=os.getenv(
                "ROLEBEACON_LLM_BASE_URL", str(llm.get("base_url", "http://127.0.0.1:11434/v1"))
            ).rstrip("/"),
            llm_model=os.getenv("ROLEBEACON_LLM_MODEL", str(llm.get("model", "qwen3:8b"))),
            llm_api_key=api_key,
            llm_timeout_seconds=float(os.getenv("ROLEBEACON_LLM_TIMEOUT_SECONDS", "120")),
            resume_renderer=str(setup.get("resume_renderer", "builtin")),
            external_resume_command=tuple(setup.get("external_resume_command", [])),
        )

    def refreshed(self) -> Settings:
        return Settings.load(self.root if self.data_dir == self.root / "data" else None)

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "applications").mkdir(exist_ok=True)
        (self.data_dir / "browser-profile").mkdir(exist_ok=True)

    def load_sources(self) -> list[SourceConfig]:
        path = self.source_config_path if self.source_config_path.exists() else self.resource_dir / "config" / "sources.json"
        return [SourceConfig.from_dict(value) for value in _read_json(path, [])]

    def load_search_profile(self) -> dict[str, Any]:
        return _read_json(self.search_profile_path, {})

    def save_search_profile(self, value: dict[str, Any]) -> Path:
        _write_private_json(self.search_profile_path, value)
        return self.search_profile_path

    def load_candidate_profile(self) -> dict[str, Any]:
        return _read_json(self.candidate_profile_path, {})

    def load_mobility_profile(self) -> dict[str, Any]:
        return _read_json(self.mobility_profile_path, {})

    def load_strategies(self) -> list[dict[str, Any]]:
        return _read_json(self.strategies_path, [])

    def load_company_registry(self) -> list[dict[str, Any]]:
        path = self.data_dir / "companies.json"
        if not path.exists():
            path = self.resource_dir / "config" / "companies.json"
        return _read_json(path, [])

    def save_setup(
        self,
        *,
        candidate: dict[str, Any],
        mobility: dict[str, Any],
        preferences: dict[str, Any],
        strategies: list[dict[str, Any]],
        enabled_source_ids: list[str],
        llm: dict[str, Any],
        activate: bool,
    ) -> Settings:
        self.ensure_directories()
        _write_private_json(self.candidate_profile_path, candidate)
        _write_private_json(self.mobility_profile_path, mobility)
        _write_private_json(self.search_profile_path, preferences)
        _write_private_json(self.strategies_path, strategies)
        sources = []
        enabled = set(enabled_source_ids)
        for source in _read_json(self.resource_dir / "config" / "sources.json", []):
            source["enabled"] = source.get("id") in enabled
            sources.append(source)
        _write_private_json(self.source_config_path, sources)
        public_llm = {key: value for key, value in llm.items() if key != "api_key"}
        _write_private_json(
            self.setup_state_path,
            {
                "schema_version": "1.0",
                "completed": True,
                "activated": activate,
                "llm": public_llm,
                "resume_renderer": "builtin",
            },
        )
        _write_private_json(self.secrets_path, {"llm_api_key": llm.get("api_key", "")})
        return replace(
            self,
            setup_complete=True,
            activated=activate,
            llm_mode=str(public_llm.get("mode", "rules")),
            llm_enabled=str(public_llm.get("mode", "rules")) != "rules",
            llm_base_url=str(public_llm.get("base_url", self.llm_base_url)).rstrip("/"),
            llm_model=str(public_llm.get("model", self.llm_model)),
            llm_api_key=str(llm.get("api_key", "")),
        )
