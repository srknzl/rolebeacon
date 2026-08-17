from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
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
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
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
    company_search_provider: str
    company_search_api_key: str
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
        company_search_api_key = os.getenv(
            "ROLEBEACON_BRAVE_SEARCH_API_KEY", str(secrets.get("brave_search_api_key", ""))
        )
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
            # The env var can still force this at deploy time; otherwise it's the persisted
            # setting a user flips from the "Prepare application" control on a job's page.
            open_browser=_boolean("ROLEBEACON_OPEN_BROWSER", bool(setup.get("open_browser", False))),
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
            company_search_provider="brave" if company_search_api_key else "none",
            company_search_api_key=company_search_api_key,
            resume_renderer=str(setup.get("resume_renderer", "builtin")),
            external_resume_command=tuple(setup.get("external_resume_command", [])),
        )

    def refreshed(self) -> Settings:
        return Settings.load(self.root if self.data_dir == self.root / "data" else None)

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "applications").mkdir(exist_ok=True)
        (self.data_dir / "browser-profile").mkdir(exist_ok=True)
        self._merge_default_sources()

    def _merge_default_sources(self) -> None:
        """Add newly shipped sources without changing a user's existing enablement choices."""
        from .collectors import COLLECTORS

        defaults = _read_json(self.resource_dir / "config" / "sources.json", [])
        current = _read_json(self.source_config_path, [])
        if not current:
            _write_private_json(self.source_config_path, defaults)
            return
        current_by_id = {str(item.get("id", "")): item for item in current if isinstance(item, dict)}
        merged = [{**item, **current_by_id.pop(str(item.get("id", "")), {})} for item in defaults]
        # A source instance whose collector kind was retired (e.g. a removed feature) can never
        # sync again; drop it here instead of leaving it to surface as a permanently-broken,
        # confusingly-labeled entry in the source list.
        merged.extend(item for item in current_by_id.values() if item.get("kind") in COLLECTORS)
        if merged != current:
            _write_private_json(self.source_config_path, merged)

    def load_sources(self) -> list[SourceConfig]:
        path = self.source_config_path if self.source_config_path.exists() else self.resource_dir / "config" / "sources.json"
        sources = [SourceConfig.from_dict(value) for value in _read_json(path, [])]
        setup = _read_json(self.setup_state_path, {})
        configuration = setup.get("configuration", {}) if isinstance(setup, dict) else {}
        enabled_ids = configuration.get("enabled_source_ids") if isinstance(configuration, dict) else None
        if isinstance(enabled_ids, list):
            enabled = {str(value) for value in enabled_ids}
            for source in sources:
                source.enabled = source.id in enabled
        return sources

    def _commit_source_enablement(self, sources: list[SourceConfig]) -> None:
        """Keep the setup manifest as the atomic authority for source enablement."""
        setup = _read_json(self.setup_state_path, {})
        if not isinstance(setup, dict) or not setup.get("completed"):
            return
        configuration = setup.get("configuration")
        if not isinstance(configuration, dict):
            configuration = {}
            setup["configuration"] = configuration
        configuration["enabled_source_ids"] = sorted(source.id for source in sources if source.enabled)
        setup["generation"] = uuid.uuid4().hex
        _write_private_json(self.setup_state_path, setup)

    def save_source(self, source: SourceConfig) -> tuple[SourceConfig, bool]:
        """Add or update one source instance without altering other source choices."""
        from .source_discovery import same_source

        self.ensure_directories()
        sources = self.load_sources()
        for index, current in enumerate(sources):
            if same_source(current, source):
                source.id = current.id
                sources[index] = source
                _write_private_json(self.source_config_path, [item.to_dict() for item in sources])
                self._commit_source_enablement(sources)
                return source, False
        existing_ids = {item.id for item in sources}
        base_id = source.id
        suffix = 2
        while source.id in existing_ids:
            source.id = f"{base_id[:94]}-{suffix}"
            suffix += 1
        sources.append(source)
        _write_private_json(self.source_config_path, [item.to_dict() for item in sources])
        self._commit_source_enablement(sources)
        return source, True

    def save_sources(self, candidates: list[SourceConfig]) -> tuple[list[SourceConfig], int]:
        """Atomically add or update source instances while preserving existing enablement."""
        from .source_discovery import same_source

        self.ensure_directories()
        sources = self.load_sources()
        existing_ids = {source.id for source in sources}
        saved: list[SourceConfig] = []
        added = 0
        for candidate in candidates:
            current_index = next(
                (index for index, current in enumerate(sources) if same_source(current, candidate)),
                None,
            )
            if current_index is not None:
                current = sources[current_index]
                candidate.id = current.id
                candidate.name = current.name or candidate.name
                candidate.enabled = current.enabled or candidate.enabled
                candidate.options = {**current.options, **candidate.options}
                sources[current_index] = candidate
                saved.append(candidate)
                continue
            base_id = candidate.id
            suffix = 2
            while candidate.id in existing_ids:
                candidate.id = f"{base_id[:94]}-{suffix}"
                suffix += 1
            existing_ids.add(candidate.id)
            sources.append(candidate)
            saved.append(candidate)
            added += 1
        _write_private_json(self.source_config_path, [source.to_dict() for source in sources])
        self._commit_source_enablement(sources)
        return saved, added

    def set_source_enabled(self, source_id: str, enabled: bool) -> SourceConfig:
        sources = self.load_sources()
        for source in sources:
            if source.id == source_id:
                source.enabled = enabled
                _write_private_json(self.source_config_path, [item.to_dict() for item in sources])
                self._commit_source_enablement(sources)
                return source
        raise LookupError(f"Source not found: {source_id}")

    def load_search_profile(self) -> dict[str, Any]:
        setup = _read_json(self.setup_state_path, {})
        configuration = setup.get("configuration", {}) if isinstance(setup, dict) else {}
        if isinstance(configuration, dict) and isinstance(configuration.get("preferences"), dict):
            return configuration["preferences"]
        return _read_json(self.search_profile_path, {})

    def save_search_profile(self, value: dict[str, Any]) -> Path:
        setup = _read_json(self.setup_state_path, {})
        configuration = setup.get("configuration") if isinstance(setup, dict) else None
        if isinstance(configuration, dict) and isinstance(configuration.get("preferences"), dict):
            updated_configuration = {**configuration, "preferences": value}
            _write_private_json(self.setup_state_path, {**setup, "configuration": updated_configuration})
            return self.setup_state_path
        _write_private_json(self.search_profile_path, value)
        return self.search_profile_path

    def load_candidate_profile(self) -> dict[str, Any]:
        setup = _read_json(self.setup_state_path, {})
        configuration = setup.get("configuration", {}) if isinstance(setup, dict) else {}
        if isinstance(configuration, dict) and isinstance(configuration.get("candidate"), dict):
            return configuration["candidate"]
        return _read_json(self.candidate_profile_path, {})

    def load_mobility_profile(self) -> dict[str, Any]:
        setup = _read_json(self.setup_state_path, {})
        configuration = setup.get("configuration", {}) if isinstance(setup, dict) else {}
        if isinstance(configuration, dict) and isinstance(configuration.get("mobility"), dict):
            return configuration["mobility"]
        return _read_json(self.mobility_profile_path, {})

    def load_strategies(self) -> list[dict[str, Any]]:
        setup = _read_json(self.setup_state_path, {})
        configuration = setup.get("configuration", {}) if isinstance(setup, dict) else {}
        if isinstance(configuration, dict) and isinstance(configuration.get("strategies"), list):
            return configuration["strategies"]
        return _read_json(self.strategies_path, [])

    def load_company_registry(self) -> list[dict[str, Any]]:
        shipped = _read_json(self.resource_dir / "config" / "companies.json", [])
        local = _read_json(self.data_dir / "companies.json", [])
        by_name = {str(item["name"]).casefold(): item for item in shipped}
        for item in local:
            by_name[str(item["name"]).casefold()] = {**by_name.get(str(item["name"]).casefold(), {}), **item}
        catalog = _read_json(self.resource_dir / "config" / "source-packs.json", {})
        for source in catalog.get("sources", []):
            key = str(source["company"]).casefold()
            entry = by_name.setdefault(key, {"name": source["company"], "domain": "", "sources": []})
            boards = entry.setdefault("job_boards", [])
            if source["url"] not in boards:
                boards.append(source["url"])
        return sorted(by_name.values(), key=lambda item: str(item["name"]).casefold())

    def save_open_browser(self, value: bool) -> Settings:
        """Persist the "open a browser and auto-fill supported fields" choice made from the
        Prepare application control so it survives restarts without needing an env var."""
        setup = _read_json(self.setup_state_path, {})
        if not isinstance(setup, dict):
            setup = {}
        self.ensure_directories()
        _write_private_json(self.setup_state_path, {**setup, "open_browser": value})
        return replace(self, open_browser=value)

    def save_company_search_key(self, api_key: str) -> Settings:
        secrets = _read_json(self.secrets_path, {})
        secrets["brave_search_api_key"] = api_key.strip()
        _write_private_json(self.secrets_path, secrets)
        return replace(
            self,
            company_search_provider="brave" if api_key.strip() else "none",
            company_search_api_key=api_key.strip(),
        )

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
        sources = self.load_sources()
        enabled = set(enabled_source_ids)
        for source in sources:
            source.enabled = source.id in enabled
        _write_private_json(self.source_config_path, [source.to_dict() for source in sources])
        public_llm = {key: value for key, value in llm.items() if key not in {"api_key", "api_key_action"}}
        secrets = _read_json(self.secrets_path, {})
        action = str(llm.get("api_key_action", "preserve"))
        if action == "preserve" and str(llm.get("api_key", "")):
            action = "replace"
        if action == "replace":
            secrets["llm_api_key"] = str(llm.get("api_key", ""))
        elif action == "remove":
            secrets.pop("llm_api_key", None)
        _write_private_json(self.secrets_path, secrets)
        _write_private_json(
            self.setup_state_path,
            {
                "schema_version": "1.0",
                "generation": uuid.uuid4().hex,
                "completed": True,
                "activated": activate,
                "llm": public_llm,
                "resume_renderer": "builtin",
                # This atomically replaced manifest is the commit point for related profile
                # files. Readers use this generation, so a crash during compatibility-file
                # writes cannot expose a mixed candidate/mobility/preferences generation.
                "configuration": {
                    "candidate": candidate,
                    "mobility": mobility,
                    "preferences": preferences,
                    "strategies": strategies,
                    "enabled_source_ids": sorted(enabled),
                },
            },
        )
        return replace(
            self,
            setup_complete=True,
            activated=activate,
            llm_mode=str(public_llm.get("mode", "rules")),
            llm_enabled=str(public_llm.get("mode", "rules")) != "rules",
            llm_base_url=str(public_llm.get("base_url", self.llm_base_url)).rstrip("/"),
            llm_model=str(public_llm.get("model", self.llm_model)),
            llm_api_key=str(secrets.get("llm_api_key", "")),
        )
