from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rolebeacon.collectors import plain_text
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob
from rolebeacon.llm import LlmClient
from rolebeacon.sync import SyncService, deduplicate_source_jobs, personalize_source


def test_incremental_window_overlaps_last_success(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    service = SyncService(settings, database, LlmClient(settings))
    last_success = datetime.now(UTC) - timedelta(days=3)

    since = service._since(last_success.isoformat())

    assert last_success - since == timedelta(hours=72)


def test_plain_text_excludes_non_visible_page_content() -> None:
    value = "<style>.secret { color: red }</style><main>Visible</main><script>hidden()</script>"

    assert plain_text(value) == "Visible"


def test_collector_duplicates_are_collapsed_before_upsert() -> None:
    first = CollectedJob(
        source="feed",
        source_job_id="1",
        title="Backend Engineer",
        company="Example",
        location="Remote",
        description="First representation",
        url="https://example.com/jobs/1",
    )
    updated = CollectedJob(
        source="feed",
        source_job_id="1",
        title="Backend Engineer",
        company="Example",
        location="Remote",
        description="Updated representation",
        url="https://example.com/jobs/1",
    )

    result = deduplicate_source_jobs([first, updated])

    assert len(result) == 1
    assert result[0].description == "Updated representation"


def test_first_party_sources_use_saved_roles_and_relocation_targets(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    google = next(source for source in settings.load_sources() if source.kind == "google_careers")
    amazon = next(source for source in settings.load_sources() if source.kind == "amazon_jobs")
    search = {"target_roles": ["Backend Engineer", "Platform Engineer"]}
    mobility = {"relocation_targets": [{"country_code": "DE", "country_name": "Germany"}]}

    personalized_google = personalize_source(google, search, mobility)
    personalized_amazon = personalize_source(amazon, search, mobility)

    assert "q=Backend+Engineer+OR+Platform+Engineer" in personalized_google.url
    assert "location=Germany" in personalized_google.url
    assert "base_query=Backend+Engineer+OR+Platform+Engineer" in personalized_amazon.url
    assert "loc_query=Germany" in personalized_amazon.url


async def test_unavailable_selected_llm_stops_refresh_before_collection_or_rules_fallback(tmp_path, monkeypatch) -> None:
    from rolebeacon.setup import SetupService

    payload = {
        "candidate": {"schema_version": "1.0", "name": "Candidate", "location": {"country_code": "TR", "country_name": "Türkiye"}, "skills": {}},
        "mobility": {"schema_version": "1.0", "current_country_code": "TR", "work_authorizations": ["TR"]},
        "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
        "enabled_source_ids": ["arbeitnow"],
        "llm": {"mode": "custom", "base_url": "http://unavailable.example/v1", "model": "missing"},
        "activate": True,
    }
    settings = SetupService(Settings.load(tmp_path)).complete(payload)
    database = Database(settings.database_path)
    database.initialize()
    service = SyncService(settings, database, LlmClient(settings))

    async def unavailable() -> dict[str, object]:
        return {"available": False, "status": "unavailable", "error": "Connection refused"}

    monkeypatch.setattr(service.llm, "health", unavailable)
    result = await service.run()

    assert result.phase == "failed"
    assert "LLM unavailable: Connection refused" in result.error
    assert "Rules only" in result.error
    assert result.jobs_seen == 0
    assert result.rule_fallback_jobs == 0
    assert database.list_sources() == []
