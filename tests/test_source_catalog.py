from __future__ import annotations

import json

from fastapi.testclient import TestClient
from test_source_workflow import setup_payload

from rolebeacon.app import create_app
from rolebeacon.config import Settings
from rolebeacon.setup import SetupService
from rolebeacon.source_catalog import SourceCatalog, validate_catalog


def configured_settings(tmp_path) -> Settings:
    return SetupService(Settings.load(tmp_path)).complete(setup_payload())


def test_source_catalog_is_valid_and_exposes_searchable_packs(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    catalog = SourceCatalog(settings)

    validate_catalog(catalog.path, settings)
    view = catalog.view()

    assert view["schema_version"] == "1.0"
    assert view["verified_at"] == "2026-08-16"
    assert len(view["sources"]) == 79
    assert {pack["id"] for pack in view["packs"]} >= {
        "big-tech",
        "developer-infrastructure",
        "tech-company-catalog",
    }
    assert {gap["company"] for gap in view["coverage_gaps"]} == {
        "Microsoft",
        "Meta",
        "Apple",
        "Netflix",
    }
    complete = next(pack for pack in view["packs"] if pack["id"] == "tech-company-catalog")
    assert complete["source_count"] == len(view["sources"])


def test_pack_installation_is_idempotent_and_preserves_enabled_sources(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    catalog = SourceCatalog(settings)

    first = catalog.install("big-tech", enabled=False)
    second = catalog.install("big-tech", enabled=True)
    third = catalog.install("big-tech", enabled=False)

    assert first.added == 8  # Google, Amazon, and Cloudflare already ship as disabled defaults.
    assert first.updated == 3
    assert first.enabled == 0
    assert second.added == 0
    assert second.enabled == 11
    assert third.added == 0
    assert third.enabled == 11
    configured = settings.load_sources()
    assert len({source.id for source in configured}) == len(configured)


def test_single_catalog_entry_install_updates_existing_board_without_duplicate(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    catalog = SourceCatalog(settings)

    source, created = catalog.install_entry("cloudflare", enabled=True)
    again, created_again = catalog.install_entry("cloudflare", enabled=False)

    assert created is False
    assert created_again is False
    assert source.id == "cloudflare"
    assert again.enabled is True
    assert sum(item.kind == "greenhouse" and item.slug == "cloudflare" for item in settings.load_sources()) == 1


def test_catalog_top_level_sources_are_not_shadowed_by_the_last_pack(tmp_path) -> None:
    settings = configured_settings(tmp_path)
    catalog = SourceCatalog(settings)
    catalog.path = tmp_path / "source-packs.json"
    catalog.path.write_text(json.dumps({
        "schema_version": "1.0",
        "verified_at": "2026-08-16",
        "packs": [
            {"id": "all", "name": "All", "description": "", "source_ids": ["cloudflare", "gitlab"]},
            {"id": "last-subset", "name": "Subset", "description": "", "source_ids": ["gitlab"]},
        ],
        "sources": [
            {"id": "cloudflare", "company": "Cloudflare", "name": "Cloudflare", "url": "https://job-boards.greenhouse.io/cloudflare", "connector": "greenhouse"},
            {"id": "gitlab", "company": "GitLab", "name": "GitLab", "url": "https://job-boards.greenhouse.io/gitlab", "connector": "greenhouse"},
        ],
    }), encoding="utf-8")

    assert {source["id"] for source in catalog.view()["sources"]} == {"cloudflare", "gitlab"}


def test_source_pack_api_and_page_work_end_to_end(tmp_path) -> None:
    app = create_app(configured_settings(tmp_path))

    with TestClient(app) as client:
        page = client.get("/sources")
        catalog = client.get("/api/source-packs")
        installed = client.post("/api/source-packs/developer-infrastructure/install", json={"enabled": False})
        enabled = client.post("/api/source-catalog/linear/install", json={"enabled": True})
        page_after = client.get("/sources")

    assert page.status_code == 200
    assert "Source packs" in page.text
    assert "Browse all 79 verified company boards" in page.text
    assert catalog.status_code == 200
    assert installed.status_code == 200
    assert installed.json()["added"] > 0
    assert installed.json()["enabled"] == 0
    assert enabled.status_code == 201
    assert enabled.json()["source"]["enabled"] is True
    assert "Linear" in page_after.text
    assert ">enabled<" in page_after.text
