from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from rolebeacon.collectors import plain_text
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob, ScoreResult, SourceConfig
from rolebeacon.llm import LlmClient, LlmResponseRejected
from rolebeacon.setup import SetupService
from rolebeacon.source_discovery import relocation_source_candidates
from rolebeacon.sync import (
    SyncService,
    _friendly_error_prefix,
    deduplicate_source_jobs,
    engineering_job,
    personalize_source,
)


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


def test_friendly_error_prefix_covers_the_common_httpx_failures() -> None:
    request = httpx.Request("GET", "https://example.test/jobs")
    rate_limited = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    server_error = httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
    client_error = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    assert "rate-limiting" in _friendly_error_prefix(rate_limited)
    assert "server had an error" in _friendly_error_prefix(server_error)
    assert "rejected the request" in _friendly_error_prefix(client_error)
    assert "too long to respond" in _friendly_error_prefix(httpx.ReadTimeout("timed out", request=request))
    assert "Could not connect" in _friendly_error_prefix(httpx.ConnectError("refused", request=request))
    assert _friendly_error_prefix(ValueError("unrelated")) == ""


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


def test_ingestion_filter_uses_only_candidate_authored_terms_and_exact_role_phrases() -> None:
    profile = {
        "target_roles": ["Software Engineer", "Backend Engineer"],
        "preferred_skills": ["Go"],
        "preferred_domains": ["distributed systems"],
    }

    assert engineering_job(
        CollectedJob(
            source="fixture", source_job_id="1", title="Software Engineer", company="Example",
            location="Remote", description="", url="https://example.test/1",
        ),
        profile,
    )
    assert not engineering_job(
        CollectedJob(
            source="fixture", source_job_id="2", title="Google Partnerships Manager", company="Example",
            location="Remote", description="", url="https://example.test/2",
        ),
        profile,
    )


def test_ingestion_filter_keeps_watchlist_companies_and_rejects_unrelated_titles() -> None:
    profile = {
        "target_roles": ["Backend Engineer"],
        "preferred_skills": ["Python"],
        "preferred_domains": ["cloud"],
        "company_watchlist": ["Watched Co"],
    }
    unrelated = CollectedJob(
        source="fixture", source_job_id="1", title="Account Executive", company="Other Co",
        location="Remote", description="", url="https://example.test/1",
    )
    watched = CollectedJob(
        source="fixture", source_job_id="2", title="Account Executive", company="Watched Co",
        location="Remote", description="", url="https://example.test/2",
    )

    assert not engineering_job(unrelated, profile)
    assert engineering_job(watched, profile)


def test_first_party_sources_use_saved_roles_and_keep_their_baked_in_location(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    google = next(source for source in settings.load_sources() if source.kind == "google_careers")
    amazon = next(source for source in settings.load_sources() if source.kind == "amazon_jobs")
    search = {"target_roles": ["Backend Engineer", "Platform Engineer"]}

    personalized_google = personalize_source(google, search)
    personalized_amazon = personalize_source(amazon, search)

    assert "q=Backend+Engineer+OR+Platform+Engineer" in personalized_google.url
    assert "location=Germany" in personalized_google.url
    assert "base_query=Backend+Engineer+OR+Platform+Engineer" in personalized_amazon.url
    assert "loc_query=Germany" in personalized_amazon.url


def test_first_party_sources_never_fall_back_to_a_hardcoded_title() -> None:
    generated = relocation_source_candidates([{"code": "DE", "name": "Germany"}])
    google = next(source for source in generated if source.kind == "google_careers")
    amazon = next(source for source in generated if source.kind == "amazon_jobs")

    personalized_google = personalize_source(google, {"target_roles": []})
    personalized_amazon = personalize_source(amazon, {"target_roles": []})

    assert "q=" not in personalized_google.url
    assert "base_query=" not in personalized_amazon.url


def test_personalize_source_never_touches_each_generated_countrys_own_location() -> None:
    # Root cause of "56 Google/56 Amazon rows, still thin results": personalize_source() used to
    # overwrite every generated row's location with whichever country came first in relocation
    # targets, on every sync - collapsing all per-country rows onto one country. It must not do
    # that again: each row's own baked-in location must survive personalizing untouched.
    generated = relocation_source_candidates([{"code": "DE", "name": "Germany"}, {"code": "FR", "name": "France"}])
    search = {"target_roles": ["Backend Engineer"]}

    personalized = [personalize_source(source, search) for source in generated]

    google_urls = [source.url for source in personalized if source.kind == "google_careers"]
    amazon_urls = [source.url for source in personalized if source.kind == "amazon_jobs"]
    assert any("location=Germany" in url for url in google_urls)
    assert any("location=France" in url for url in google_urls)
    assert any("loc_query=Germany" in url for url in amazon_urls)
    assert any("loc_query=France" in url for url in amazon_urls)


def test_personalize_source_injects_role_text_into_option_based_query_kinds() -> None:
    # Adzuna, Jooble, SerpApi, and Remotive store their free-text query in options, not the URL -
    # each must receive the candidate's real target_roles the same way Google/Amazon do.
    search = {"target_roles": ["Backend Engineer", "Platform Engineer"]}
    for kind, option_key in (("adzuna", "query"), ("jooble", "query"), ("serpapi", "query"), ("remotive", "search")):
        source = SourceConfig(id=f"{kind}-test", kind=kind, name=kind)

        personalized = personalize_source(source, search)

        assert personalized.options[option_key] == "Backend Engineer OR Platform Engineer", kind


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


async def test_llm_response_rejected_falls_back_to_rules_for_just_that_job(tmp_path, monkeypatch) -> None:
    payload = {
        "candidate": {"schema_version": "1.0", "name": "Candidate", "location": {"country_code": "TR", "country_name": "Türkiye"}, "skills": {}},
        "mobility": {"schema_version": "1.0", "current_country_code": "TR", "work_authorizations": ["TR"]},
        "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
        "enabled_source_ids": [],
        "llm": {"mode": "custom", "base_url": "http://model.example/v1", "model": "test-model"},
        "activate": True,
    }
    settings = SetupService(Settings.load(tmp_path)).complete(payload)
    database = Database(settings.database_path)
    database.initialize()
    job_id, _ = database.upsert_job(
        CollectedJob(
            source="fixture", source_job_id="1", title="Backend Engineer", company="Example",
            location="Remote Worldwide", description="Build backend systems", url="https://example.com/jobs/1",
        )
    )
    service = SyncService(settings, database, LlmClient(settings))

    async def available() -> dict[str, object]:
        return {"available": True, "status": "available", "error": ""}

    async def rejected(*_args, **_kwargs) -> ScoreResult:
        raise LlmResponseRejected("generic gap label stack")

    monkeypatch.setattr(service.llm, "health", available)
    monkeypatch.setattr(service.llm, "score", rejected)
    result = await service.run()

    assert result.phase == "complete"
    assert result.rule_fallback_jobs == 1
    assert database.get_job(job_id)["provider"] == "rules"
