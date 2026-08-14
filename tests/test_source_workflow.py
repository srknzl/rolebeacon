from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from rolebeacon.app import create_app
from rolebeacon.collectors import AmazonJobsCollector, GoogleCareersCollector
from rolebeacon.config import Settings
from rolebeacon.domain import SourceConfig
from rolebeacon.setup import SetupService
from rolebeacon.source_discovery import (
    SourceDiscoveryError,
    SourceDiscoveryService,
    amazon_search_params,
    detect_source,
    same_source,
)


def setup_payload() -> dict:
    return {
        "candidate": {
            "schema_version": "1.0",
            "name": "Example Candidate",
            "headline": "Backend Engineer",
            "summary": "Builds reliable backend systems.",
            "contact": {"email": "candidate@example.com", "phone": ""},
            "location": {"country_code": "TR", "country_name": "Türkiye", "city": "Istanbul"},
            "skills": {"Languages": ["Python", "Go"]},
            "experience": [],
            "projects": [],
            "education": [],
            "languages": [],
        },
        "mobility": {
            "schema_version": "1.0",
            "current_country_code": "TR",
            "work_authorizations": ["TR"],
            "relocation_targets": [{"country_code": "DE", "country_name": "Germany", "cities": []}],
        },
        "preferences": {
            "schema_version": "1.0",
            "target_roles": ["Backend Engineer"],
            "preferred_skills": ["Python", "Go"],
            "priority_companies": ["Example Co"],
        },
        "enabled_source_ids": [],
        "llm": {"mode": "rules", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"},
        "activate": True,
    }


@pytest.mark.parametrize(
    ("url", "kind", "expected"),
    [
        ("https://boards.greenhouse.io/acme", "greenhouse", {"slug": "acme"}),
        ("https://jobs.lever.co/acme", "lever", {"slug": "acme", "host": "https://api.lever.co"}),
        ("https://jobs.eu.lever.co/acme", "lever", {"slug": "acme", "host": "https://api.eu.lever.co"}),
        ("https://jobs.ashbyhq.com/acme", "ashby", {"slug": "acme"}),
        ("https://jobs.smartrecruiters.com/Acme", "smartrecruiters", {"slug": "Acme"}),
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/External",
            "workday",
            {"host": "https://acme.wd5.myworkdayjobs.com", "tenant": "acme", "site": "External"},
        ),
        (
            "https://www.google.com/about/careers/applications/jobs/results/?q=Software+Engineer&location=Germany",
            "google_careers",
            {"url": "https://www.google.com/about/careers/applications/jobs/results/?q=Software+Engineer&location=Germany"},
        ),
        (
            "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Germany",
            "amazon_jobs",
            {"url": "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Germany"},
        ),
    ],
)
def test_detect_source_extracts_supported_public_ats_instances(
    url: str, kind: str, expected: dict[str, str]
) -> None:
    source = detect_source(url, "Acme")

    assert source.kind == kind
    assert source.company == "Acme"
    assert source.trust_priority == 100
    assert source.options["careers_url"] == url
    for key, value in expected.items():
        assert getattr(source, key) == value


@pytest.mark.parametrize(
    "url",
    [
        "http://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io",
        "https://example.test/careers",
    ],
)
def test_detect_source_rejects_untrusted_or_incomplete_urls(url: str) -> None:
    with pytest.raises(SourceDiscoveryError):
        detect_source(url, "Acme")


def test_detect_source_explains_when_a_first_party_connector_is_still_missing() -> None:
    with pytest.raises(SourceDiscoveryError, match="dedicated first-party connector"):
        detect_source("https://apply.careers.microsoft.com/careers", "Microsoft")


def test_first_party_searches_are_deduplicated_by_query_not_only_connector_kind() -> None:
    germany = detect_source(
        "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Germany",
        "Amazon",
    )
    reordered = detect_source(
        "https://www.amazon.jobs/en/search?loc_query=Germany&base_query=software+engineer",
        "Amazon",
    )
    united_states = detect_source(
        "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=United+States",
        "Amazon",
    )

    assert same_source(germany, reordered) is True
    assert same_source(germany, united_states) is False
    assert germany.options["location_filter_code"] == "DE"


@pytest.mark.asyncio
async def test_source_preview_validates_the_endpoint_and_returns_sample_jobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "boards-api.greenhouse.io"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 1,
                        "title": "Backend Engineer",
                        "location": {"name": "Berlin"},
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                    },
                    {
                        "id": 2,
                        "title": "Platform Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                    },
                ]
            },
        )

    service = SourceDiscoveryService(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    preview = await service.preview("https://boards.greenhouse.io/acme", "Acme")

    assert preview.jobs_found == 2
    assert preview.source.kind == "greenhouse"
    assert [job["title"] for job in preview.sample_jobs] == ["Backend Engineer", "Platform Engineer"]


@pytest.mark.asyncio
async def test_amazon_preview_applies_location_filter_before_showing_samples() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/en/search.json"
        return httpx.Response(
            200,
            json={
                "hits": 2000,
                "jobs": [
                    {"title": "US Engineer", "location": "US, WA, Seattle", "job_path": "/en/jobs/1/us"},
                    {"title": "Berlin Engineer", "location": "DE, BE, Berlin", "job_path": "/en/jobs/2/de"},
                ],
            },
        )

    service = SourceDiscoveryService(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    preview = await service.preview(
        "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Germany",
        "Amazon",
    )

    assert preview.jobs_found == 1
    assert [job["title"] for job in preview.sample_jobs] == ["Berlin Engineer"]
    assert "location-matched jobs" in preview.message


@pytest.mark.asyncio
async def test_google_first_party_collector_parses_public_search_and_detail_pages() -> None:
    search_html = """
    <span>1 jobs matched</span>
    <a href="jobs/results/123-senior-backend-engineer"
       aria-label="Learn more about Senior Backend Engineer"></a>
    """
    detail_html = """
    <h1>Senior Backend Engineer</h1>
    <span>Google place Berlin, Germany bar_chart Mid</span>
    <h3>Minimum qualifications:</h3><p>Build distributed Java systems.</p>
    <p>Information collected and processed as part of your Google Careers profile</p>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=detail_html if "123-" in request.url.path else search_html)

    config = detect_source(
        "https://www.google.com/about/careers/applications/jobs/results/?q=Backend&location=Germany",
        "Google",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await GoogleCareersCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    assert batch.requests_made == 3
    assert len(batch.jobs) == 1
    assert batch.jobs[0].source_job_id == "123"
    assert batch.jobs[0].location == "Berlin, Germany"
    assert "distributed Java systems" in batch.jobs[0].description


@pytest.mark.asyncio
async def test_amazon_first_party_collector_normalizes_public_json_jobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/en/search.json"
        # Without an ISO alpha-3 "country" param, amazon.jobs geo-filters weakly and a page
        # sorted by "recent" across every country returns almost no matches for one.
        assert request.url.params.get("country") == "DEU"
        return httpx.Response(
            200,
            json={
                "hits": 1,
                "jobs": [
                    {
                        "id_icims": "455",
                        "title": "Software Development Engineer",
                        "location": "US, WA, Seattle",
                        "description": "Build unrelated services.",
                        "job_path": "/en/jobs/455/software-development-engineer",
                        "posted_date": datetime.now(UTC).strftime("%B %d, %Y"),
                    },
                    {
                        "id_icims": "456",
                        "title": " Software Development Engineer",
                        "location": "DE, Berlin",
                        "description": "Build distributed services.",
                        "basic_qualifications": "Experience with Java.",
                        "job_path": "/en/jobs/456/software-development-engineer",
                        "posted_date": datetime.now(UTC).strftime("%B %d, %Y"),
                    }
                ],
            },
        )

    config = SourceConfig.from_dict(
        {
            **detect_source(
                "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Germany", "Amazon"
            ).to_dict(),
            "max_pages": 1,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await AmazonJobsCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    assert batch.requests_made == 1
    assert len(batch.jobs) == 1
    assert batch.jobs[0].source_job_id == "456"
    assert batch.jobs[0].title == "Software Development Engineer"
    assert batch.jobs[0].url == "https://www.amazon.jobs/en/jobs/456/software-development-engineer"
    assert "Experience with Java" in batch.jobs[0].description


def test_setup_edits_preserve_user_added_source_instances(tmp_path) -> None:
    payload = setup_payload()
    settings = SetupService(Settings.load(tmp_path)).complete(payload)
    source, created = settings.save_source(detect_source("https://jobs.lever.co/acme", "Acme"))
    payload["enabled_source_ids"] = [source.id]

    updated = SetupService(settings).complete(payload)

    assert created is True
    saved = next(item for item in updated.load_sources() if item.id == source.id)
    assert saved.enabled is True
    assert saved.kind == "lever"
    assert saved.options["managed_by"] == "user"


def test_sources_workflow_discovers_saves_syncs_and_displays_jobs_end_to_end(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "boards-api.greenhouse.io"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 42,
                        "title": "Senior Backend Engineer",
                        "location": {"name": "Berlin, Germany"},
                        "content": "Build Python and Go distributed systems. Visa sponsorship is available.",
                        "absolute_url": "https://boards.greenhouse.io/example/jobs/42",
                        "updated_at": now,
                        "metadata": [],
                    }
                ]
            },
        )

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    settings = SetupService(Settings.load(tmp_path)).complete(setup_payload())
    app = create_app(settings)
    app.state.source_discovery.client_factory = client_factory
    monkeypatch.setattr("rolebeacon.sync.default_http_client", client_factory)

    with TestClient(app) as client:
        page_before = client.get("/sources")
        discovered = client.post(
            "/api/sources/discover",
            json={"company": "Example Co", "careers_url": "https://boards.greenhouse.io/example"},
        )
        added = client.post(
            "/api/sources",
            json={
                "company": "Example Co",
                "careers_url": "https://boards.greenhouse.io/example",
                "enabled": True,
            },
        )
        page_after = client.get("/sources")
        sync = client.post("/api/sync")
        jobs = client.get("/api/jobs")

    assert page_before.status_code == 200
    assert "Connect a careers board" in page_before.text
    assert discovered.status_code == 200
    assert discovered.json()["sample_jobs"][0]["title"] == "Senior Backend Engineer"
    assert added.status_code == 201
    assert added.json()["source"]["enabled"] is True
    assert "Example Co" in page_after.text
    assert "covered" in page_after.text
    assert sync.status_code == 202
    assert jobs.json()["jobs"][0]["title"] == "Senior Backend Engineer"
    assert jobs.json()["jobs"][0]["primary_source_id"].startswith("greenhouse-example-co")


def test_amazon_search_params_derives_the_iso_alpha3_country_from_the_location_filter() -> None:
    # amazon.jobs only honors geography server-side through "country" (ISO alpha-3). Without it a
    # request sorted by "recent" scans every country and almost never lands on the one requested.
    params = amazon_search_params(
        "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Germany", 0, 100, "DE"
    )

    assert params["country"] == "DEU"


def test_amazon_search_params_does_not_override_a_country_already_in_the_url() -> None:
    params = amazon_search_params(
        "https://www.amazon.jobs/en/search?base_query=software+engineer&country=USA", 0, 100, "DE"
    )

    assert params["country"] == "USA"
