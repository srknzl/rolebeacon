from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from rolebeacon.app import create_app
from rolebeacon.collectors import AmazonJobsCollector, GoogleCareersCollector, _google_job_detail
from rolebeacon.config import Settings
from rolebeacon.domain import SourceConfig
from rolebeacon.profile import (
    CONTINENT_COUNTRY_CODES,
    RELOCATION_REGION_CODES,
    SETUP_PLANNING_PROMPT,
    relocation_countries,
)
from rolebeacon.setup import SetupService
from rolebeacon.source_discovery import (
    SourceDiscoveryError,
    SourceDiscoveryService,
    amazon_search_params,
    detect_source,
    relocation_source_candidates,
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
            "https://acme.jobs.personio.com/",
            "personio",
            {"host": "https://acme.jobs.personio.com", "slug": "acme"},
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
async def test_personio_preview_uses_the_public_xml_feed() -> None:
    xml = """
    <workzag-jobs><position><id>42</id><office>Berlin</office><name>Platform Engineer</name></position></workzag-jobs>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://acme.jobs.personio.com/xml")
        return httpx.Response(200, content=xml)

    service = SourceDiscoveryService(lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    preview = await service.preview("https://acme.jobs.personio.com/", "Acme")

    assert preview.jobs_found == 1
    assert preview.source.kind == "personio"
    assert preview.sample_jobs == [{
        "title": "Platform Engineer",
        "location": "Berlin",
        "url": "https://acme.jobs.personio.com/job/42?display=en",
    }]


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


def test_google_job_detail_parses_the_current_no_icon_location_format() -> None:
    # Newer postings drop the icon-label markup entirely: just "Google <location>" on its own
    # line right after the title, no "place"/"bar_chart" words at all.
    text = "Senior Backend Engineer\nGoogle Sunnyvale, CA, USA\nMinimum qualifications:\nBuild systems."

    detail = _google_job_detail(text, "Senior Backend Engineer")

    assert detail["location"] == "Sunnyvale, CA, USA"


def test_google_job_detail_ignores_a_google_mention_inside_the_title() -> None:
    # A title like "Engineer, Google Cloud" contains "Google" itself - the real location line
    # that follows the title must win, not a false match inside the title text.
    text = "Engineer, Google Cloud\nGoogle Zurich, Switzerland\nMinimum qualifications:\nBuild systems."

    detail = _google_job_detail(text, "Engineer, Google Cloud")

    assert detail["location"] == "Zurich, Switzerland"


def test_google_job_detail_rejects_prose_swallowed_from_an_about_us_blurb() -> None:
    # Some teams (DeepMind, Ads...) prepend an "about us" blurb mentioning Google before any real
    # location line - the naive first-match would capture a sentence instead of a place, so a
    # safe empty result is required rather than surfacing prose as a location.
    text = (
        "Research Scientist\n"
        "We are Google DeepMind, and we are on a mission to solve intelligence.\n"
        "Minimum qualifications:\nBuild systems."
    )

    detail = _google_job_detail(text, "Research Scientist")

    assert detail["location"] == ""


def _google_search_html(page: int, jobs: list[tuple[str, str]]) -> str:
    links = "\n".join(
        f'<a href="jobs/results/{job_id}-{title.lower().replace(" ", "-")}"'
        f' aria-label="Learn more about {title}"></a>'
        for job_id, title in jobs
    )
    return f"<span>page {page}</span>\n{links}"


def _google_detail_html(title: str) -> str:
    return (
        f"<h1>{title}</h1>\n"
        "<span>Google place Berlin, Germany bar_chart Mid</span>\n"
        "<h3>Minimum qualifications:</h3><p>Build distributed systems.</p>\n"
        "<p>Information collected and processed as part of your Google Careers profile</p>"
    )


@pytest.mark.asyncio
async def test_google_first_party_collector_collects_every_job_across_all_ten_pages() -> None:
    # Correctness check requested directly: fabricate a query whose results span every one of
    # the 10 allowed pages, independently count every job link across those pages the way a
    # human paging through the real site would, and assert the collector drops none of them.
    pages = {
        page: [(str(page * 10 + slot), f"Engineer {page}-{slot}") for slot in range(3)] for page in range(1, 11)
    }
    reference_ids = {job_id for jobs in pages.values() for job_id, _ in jobs}
    assert len(reference_ids) == 30  # 10 pages x 3 jobs/page, sanity-checking the fixture itself
    titles_by_id = {job_id: title for jobs in pages.values() for job_id, title in jobs}

    def handler(request: httpx.Request) -> httpx.Response:
        detail_match = re.search(r"/jobs/results/(\d+)", request.url.path)
        if detail_match and detail_match.group(1) in titles_by_id:
            return httpx.Response(200, text=_google_detail_html(titles_by_id[detail_match.group(1)]))
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, text=_google_search_html(page, pages.get(page, [])))

    config = detect_source(
        "https://www.google.com/about/careers/applications/jobs/results/?q=Engineer&location=Germany", "Google"
    )
    config = SourceConfig.from_dict({**config.to_dict(), "max_pages": 10})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await GoogleCareersCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    collected_ids = {job.source_job_id for job in batch.jobs}
    assert collected_ids == reference_ids, f"missing: {reference_ids - collected_ids}"


@pytest.mark.asyncio
async def test_google_first_party_collector_keeps_already_found_jobs_when_a_later_page_fails() -> None:
    # A slow-walked/rate-limited later page must not silently discard jobs already found on the
    # pages before it - that would report success with 0 saved even though the work was real.
    def handler(request: httpx.Request) -> httpx.Response:
        if "jobs/results/1-" in request.url.path:
            return httpx.Response(200, text=_google_detail_html("Engineer One"))
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, text=_google_search_html(1, [("1", "Engineer One")]))
        raise httpx.ReadTimeout("simulated provider stall", request=request)

    config = SourceConfig.from_dict(
        {
            **detect_source(
                "https://www.google.com/about/careers/applications/jobs/results/?q=Engineer&location=Germany",
                "Google",
            ).to_dict(),
            "max_pages": 10,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await GoogleCareersCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    assert [job.source_job_id for job in batch.jobs] == ["1"]


@pytest.mark.asyncio
async def test_google_first_party_collector_keeps_other_jobs_when_one_detail_page_fails() -> None:
    # One flaky job detail fetch must not cost every other job found on the same page.
    def handler(request: httpx.Request) -> httpx.Response:
        if "jobs/results/1-" in request.url.path:
            raise httpx.ReadTimeout("simulated provider stall", request=request)
        if "jobs/results/2-" in request.url.path:
            return httpx.Response(200, text=_google_detail_html("Engineer Two"))
        return httpx.Response(
            200, text=_google_search_html(1, [("1", "Engineer One"), ("2", "Engineer Two")])
        )

    config = detect_source(
        "https://www.google.com/about/careers/applications/jobs/results/?q=Engineer&location=Germany", "Google"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await GoogleCareersCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    assert [job.source_job_id for job in batch.jobs] == ["2"]


@pytest.mark.asyncio
async def test_amazon_first_party_collector_keeps_already_found_jobs_when_a_later_page_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(
                200,
                json={
                    "hits": 300,
                    "jobs": [
                        {
                            "id_icims": "1", "title": "Engineer One", "location": "DE, Berlin",
                            "job_path": "/en/jobs/1", "posted_date": datetime.now(UTC).strftime("%B %d, %Y"),
                        }
                    ],
                },
            )
        raise httpx.ReadTimeout("simulated provider stall", request=request)

    config = SourceConfig.from_dict(
        {
            **detect_source(
                "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Germany", "Amazon"
            ).to_dict(),
            "max_pages": 10,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await AmazonJobsCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    assert [job.source_job_id for job in batch.jobs] == ["1"]


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


@pytest.mark.asyncio
async def test_amazon_first_party_collector_stops_at_the_first_empty_page() -> None:
    # A generated per-country row now allows up to 10 pages so large countries aren't cut short,
    # but a country with no matches must not spend all 10 requests finding that out.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": 0, "jobs": []})

    config = SourceConfig.from_dict(
        {
            **detect_source(
                "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Iceland", "Amazon"
            ).to_dict(),
            "max_pages": 10,
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await AmazonJobsCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    assert batch.requests_made == 1
    assert batch.jobs == []


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


def test_relocation_region_codes_and_continent_country_codes_stay_in_sync() -> None:
    # Guards against the two maps silently drifting apart as continents are added or renamed.
    assert set(RELOCATION_REGION_CODES) == set(CONTINENT_COUNTRY_CODES)


def test_setup_planning_prompt_lists_every_canonical_supported_region() -> None:
    for code, name in RELOCATION_REGION_CODES.items():
        assert f"`{code}` ({name})" in SETUP_PLANNING_PROMPT
    assert "may additionally use `EUROPE`" not in SETUP_PLANNING_PROMPT


def test_relocation_countries_expands_a_continent_and_dedupes_an_explicit_member() -> None:
    countries = relocation_countries([
        {"country_code": "OCEANIA", "country_name": "Oceania", "cities": []},
        {"country_code": "AU", "country_name": "Australia", "cities": []},
    ])

    codes = [item["code"] for item in countries]
    assert set(codes) == set(CONTINENT_COUNTRY_CODES["OCEANIA"])
    assert codes.count("AU") == 1


def test_relocation_source_candidates_carry_no_role_text() -> None:
    candidates = relocation_source_candidates([{"code": "FR", "name": "France"}])

    google = next(item for item in candidates if item.kind == "google_careers")
    amazon = next(item for item in candidates if item.kind == "amazon_jobs")
    assert "q=" not in google.url and "location=France" in google.url
    assert "base_query=" not in amazon.url and "loc_query=France" in amazon.url
    assert amazon.options["location_filter_code"] == "FR"
    assert google.max_pages == 10
    assert amazon.max_pages == 10


def test_relocation_source_candidates_are_named_per_country_not_identically() -> None:
    # A user with several relocation-target countries gets dozens of Google/Amazon rows on the
    # Sources health table; each must be traceable to its country, not just the first one.
    candidates = relocation_source_candidates(
        [{"code": "FR", "name": "France"}, {"code": "DE", "name": "Germany"}]
    )

    names = [item.name for item in candidates]
    assert names == ["Google Careers — France", "Amazon Jobs — France", "Google Careers — Germany", "Amazon Jobs — Germany"]
    assert len(set(names)) == len(names)


def test_relocation_source_candidates_germany_entry_matches_the_shipped_default() -> None:
    # personalize_source() overwrites q/base_query at every sync, so continuity for existing
    # users depends only on the location half of the URL matching the shipped default's.
    default_google = detect_source(
        "https://www.google.com/about/careers/applications/jobs/results/?q=Software+Engineer&location=Germany",
        "Google",
    )
    default_amazon = detect_source(
        "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=Germany", "Amazon"
    )
    generated = relocation_source_candidates([{"code": "DE", "name": "Germany"}])
    generated_google = next(item for item in generated if item.kind == "google_careers")
    generated_amazon = next(item for item in generated if item.kind == "amazon_jobs")

    assert same_source(default_google, generated_google)
    assert same_source(default_amazon, generated_amazon)


def test_setup_complete_expands_a_continent_toggle_into_every_member_country(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    payload = setup_payload()
    payload["mobility"]["relocation_targets"] = [{"country_code": "OCEANIA", "country_name": "Oceania", "cities": []}]
    payload["enabled_source_ids"] = ["__google_careers__", "__amazon_jobs__"]

    updated = SetupService(settings).complete(payload)

    enabled = {source.id for source in updated.load_sources() if source.enabled}
    google_enabled = {s for s in enabled if s.startswith("google-careers-")}
    amazon_enabled = {s for s in enabled if s.startswith("amazon-jobs-")}
    assert len(google_enabled) == len(CONTINENT_COUNTRY_CODES["OCEANIA"])
    assert len(amazon_enabled) == len(CONTINENT_COUNTRY_CODES["OCEANIA"])


def test_setup_complete_disables_generated_sources_dropped_from_relocation_targets(tmp_path) -> None:
    settings = Settings.load(tmp_path)
    first = setup_payload()
    first["mobility"]["relocation_targets"] = [
        {"country_code": "DE", "country_name": "Germany", "cities": []},
        {"country_code": "FR", "country_name": "France", "cities": []},
    ]
    first["enabled_source_ids"] = ["__google_careers__"]
    settings = SetupService(settings).complete(first)
    assert any(s.id == "google-careers-france" and s.enabled for s in settings.load_sources())

    second = setup_payload()
    second["mobility"]["relocation_targets"] = [{"country_code": "DE", "country_name": "Germany", "cities": []}]
    second["enabled_source_ids"] = ["__google_careers__"]
    settings = SetupService(settings).complete(second)

    france = next(s for s in settings.load_sources() if s.id == "google-careers-france")
    germany = next(s for s in settings.load_sources() if s.id == "google-careers-germany")
    assert not france.enabled
    assert germany.enabled
