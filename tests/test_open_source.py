from __future__ import annotations

import sqlite3

from rolebeacon.config import Settings
from rolebeacon.migration import import_legacy
from rolebeacon.resume import BuiltinResumeRenderer, render_resume_html


def test_legacy_import_is_copy_only_and_idempotent(tmp_path) -> None:
    legacy = tmp_path / "legacy"
    legacy_data = legacy / "data"
    legacy_data.mkdir(parents=True)
    connection = sqlite3.connect(legacy_data / "job-radar.sqlite3")
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.execute("INSERT INTO marker VALUES ('legacy')")
    connection.commit()
    connection.close()
    application = legacy_data / "applications" / "42"
    application.mkdir(parents=True)
    (application / "resume.pdf").write_bytes(b"legacy resume")
    (legacy / ".env").write_text("JOB_RADAR_PORT=9999\nLLM_API_KEY=must-not-import\n", encoding="utf-8")
    settings = Settings.load(tmp_path / "new")

    first = import_legacy(settings, legacy)
    second = import_legacy(settings, legacy)

    assert first["database"] == "copied"
    assert second["database"] == "already_imported"
    assert (legacy_data / "job-radar.sqlite3").exists()
    assert (application / "resume.pdf").exists()
    assert (settings.data_dir / "applications" / "42" / "resume.pdf").exists()
    assert first["environment"] == {"ROLEBEACON_PORT": "9999"}
    assert "must-not-import" not in (settings.data_dir / "legacy-import.json").read_text(encoding="utf-8")


def test_default_registries_include_first_party_and_budgeted_sources(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    settings.ensure_directories()

    source_ids = {source.id for source in settings.load_sources()}
    company_names = {item["name"] for item in settings.load_company_registry()}

    assert {
        "arbeitnow-sponsored",
        "cloudflare",
        "gitlab",
        "google-careers-germany",
        "amazon-jobs-germany",
        "adzuna-germany",
        "jooble-remote",
        "serpapi-google-jobs-germany",
    } <= source_ids
    assert {"Google", "Microsoft", "Cloudflare", "GitLab", "SAP", "Zalando"} <= company_names


def test_source_pack_catalog_is_included_in_package_resources(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    catalog = settings.resource_dir / "config" / "source-packs.json"

    assert catalog.exists()
    assert '"tech-company-catalog"' in catalog.read_text(encoding="utf-8")


def test_builtin_resume_uses_only_profile_facts() -> None:
    profile = {
        "name": "Example Candidate",
        "headline": "Backend Engineer",
        "summary": "Builds reliable systems.",
        "contact": {"email": "candidate@example.com"},
        "location": {"country_name": "Türkiye", "city": "Istanbul"},
        "skills": {"Languages": ["Python", "Go"]},
        "experience": [
            {
                "company": "Example Co",
                "title": "Engineer",
                "start": "2023",
                "end": "2024",
                "highlights": ["Built an internal service."],
            }
        ],
    }

    value = render_resume_html(profile, {"title": "Go Engineer", "description": "Go services"})

    assert "Example Candidate" in value
    assert "Built an internal service." in value
    assert "Go, Python" in value
    assert "visa sponsorship" not in value.casefold()


async def test_builtin_resume_creates_pdf(tmp_path) -> None:
    profile = {
        "name": "Example Candidate",
        "headline": "Backend Engineer",
        "summary": "Builds reliable systems.",
        "contact": {"email": "candidate@example.com"},
        "location": {"country_name": "Türkiye", "city": "Istanbul"},
        "skills": {"Languages": ["Python", "Go"]},
        "experience": [],
    }

    path = await BuiltinResumeRenderer().render(
        profile=profile,
        job={"title": "Go Engineer", "description": "Go services"},
        output_dir=tmp_path / "application",
    )

    assert path.read_bytes().startswith(b"%PDF")
    assert (path.parent / "resume.html").exists()
    assert (path.parent / "resume.json").exists()
