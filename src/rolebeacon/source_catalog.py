from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .domain import SourceConfig
from .source_discovery import detect_source, same_source


class SourceCatalogError(ValueError):
    """Raised when a source pack or catalog entry is invalid."""


@dataclass(slots=True)
class PackInstallResult:
    pack_id: str
    added: int
    updated: int
    enabled: int
    source_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "added": self.added,
            "updated": self.updated,
            "enabled": self.enabled,
            "source_ids": self.source_ids,
        }


class SourceCatalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.resource_dir / "config" / "source-packs.json"

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def view(self) -> dict[str, Any]:
        catalog = self.load()
        configured = self.settings.load_sources()
        entries = {entry["id"]: entry for entry in catalog["sources"]}
        source_views = [self._entry_view(entry, catalog, configured) for entry in catalog["sources"]]
        packs = []
        for pack in catalog["packs"]:
            installed = 0
            enabled = 0
            pack_source_views = []
            for entry_id in pack["source_ids"]:
                entry = entries[entry_id]
                candidate = self._source(entry, catalog)
                current = next((source for source in configured if same_source(source, candidate)), None)
                installed += int(current is not None)
                enabled += int(bool(current and current.enabled))
                pack_source_views.append(
                    {**entry, "installed": current is not None, "enabled": bool(current and current.enabled)}
                )
            packs.append(
                {
                    **pack,
                    "source_count": len(pack["source_ids"]),
                    "installed_count": installed,
                    "enabled_count": enabled,
                    "sources": pack_source_views,
                }
            )
        return {
            "schema_version": catalog["schema_version"],
            "verified_at": catalog["verified_at"],
            "packs": packs,
            "sources": source_views,
            "coverage_gaps": catalog.get("coverage_gaps", []),
        }

    def install(self, pack_id: str, *, enabled: bool = False) -> PackInstallResult:
        catalog = self.load()
        pack = next((item for item in catalog["packs"] if item["id"] == pack_id), None)
        if not pack:
            raise SourceCatalogError(f"Unknown source pack: {pack_id}")
        entries = {entry["id"]: entry for entry in catalog["sources"]}
        candidates = [self._source(entries[entry_id], catalog, enabled=enabled, pack_id=pack_id) for entry_id in pack["source_ids"]]
        saved, added = self.settings.save_sources(candidates)
        return PackInstallResult(
            pack_id=pack_id,
            added=added,
            updated=len(saved) - added,
            enabled=sum(source.enabled for source in saved),
            source_ids=[source.id for source in saved],
        )

    def install_entry(self, entry_id: str, *, enabled: bool = False) -> tuple[SourceConfig, bool]:
        catalog = self.load()
        entry = next((item for item in catalog["sources"] if item["id"] == entry_id), None)
        if not entry:
            raise SourceCatalogError(f"Unknown source catalog entry: {entry_id}")
        source = self._source(entry, catalog, enabled=enabled)
        current = next(
            (item for item in self.settings.load_sources() if same_source(item, source)),
            None,
        )
        if current:
            source.enabled = current.enabled or enabled
            source.options = {**current.options, **source.options}
        return self.settings.save_source(source)

    @staticmethod
    def _entry_view(
        entry: dict[str, Any],
        catalog: dict[str, Any],
        configured: list[SourceConfig],
    ) -> dict[str, Any]:
        candidate = SourceCatalog._source(entry, catalog)
        current = next((source for source in configured if same_source(source, candidate)), None)
        return {**entry, "installed": current is not None, "enabled": bool(current and current.enabled)}

    @staticmethod
    def _source(
        entry: dict[str, Any],
        catalog: dict[str, Any],
        *,
        enabled: bool = False,
        pack_id: str = "",
    ) -> SourceConfig:
        source = detect_source(str(entry["url"]), str(entry["company"]))
        if source.kind != entry["connector"]:
            raise SourceCatalogError(
                f"Catalog connector mismatch for {entry['id']}: expected {entry['connector']}, detected {source.kind}"
            )
        source.name = str(entry["name"])
        source.enabled = enabled
        source.options.update(
            {
                "catalog_entry_id": entry["id"],
                "catalog_schema_version": catalog["schema_version"],
                "catalog_verified_at": catalog["verified_at"],
            }
        )
        if pack_id:
            source.options["installed_from_pack"] = pack_id
        return source


def validate_catalog(path: Path, settings: Settings) -> None:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    entries = {entry["id"]: entry for entry in catalog["sources"]}
    if len(entries) != len(catalog["sources"]):
        raise SourceCatalogError("Catalog source IDs must be unique")
    pack_ids = [str(pack["id"]) for pack in catalog["packs"]]
    if len(pack_ids) != len(set(pack_ids)):
        raise SourceCatalogError("Catalog pack IDs must be unique")
    detected: list[tuple[str, SourceConfig]] = []
    urls: set[str] = set()
    for entry in entries.values():
        url = str(entry["url"])
        if url in urls:
            raise SourceCatalogError(f"Catalog source URLs must be unique: {url}")
        urls.add(url)
        source = SourceCatalog._source(entry, catalog)
        duplicate = next((entry_id for entry_id, current in detected if same_source(current, source)), None)
        if duplicate:
            raise SourceCatalogError(f"Catalog entries {duplicate} and {entry['id']} resolve to the same source")
        detected.append((str(entry["id"]), source))
    for pack in catalog["packs"]:
        if len(pack["source_ids"]) != len(set(pack["source_ids"])):
            raise SourceCatalogError(f"Pack {pack['id']} contains duplicate source IDs")
        missing = set(pack["source_ids"]) - set(entries)
        if missing:
            raise SourceCatalogError(f"Pack {pack['id']} references unknown sources: {sorted(missing)}")
    complete = next((pack for pack in catalog["packs"] if pack["id"] == "tech-company-catalog"), None)
    if not complete or set(complete["source_ids"]) != set(entries):
        raise SourceCatalogError("The complete tech company catalog pack must contain every source entry")
