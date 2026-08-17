from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rolebeacon.collectors import (
    ArbeitnowCollector,
    AshbyCollector,
    GreenhouseCollector,
    HimalayasCollector,
    JobicyCollector,
    PersonioCollector,
    RemoteOkCollector,
    RemotiveCollector,
    SmartRecruitersCollector,
    description_blocks,
    plain_text,
    repair_text,
)
from rolebeacon.domain import SourceConfig


@pytest.mark.asyncio
async def test_arbeitnow_preserves_sponsorship_signal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["visa_sponsorship"] == "true"
        return httpx.Response(200, json={"data": [{
            "slug": "backend-1", "title": "Backend Engineer", "company_name": "Example GmbH",
            "location": "Berlin", "description": "<p>Build APIs</p>", "url": "https://example.test/1",
            "remote": False, "visa_sponsorship": True, "created_at": 1786406400,
        }], "links": {"next": None}})

    config = SourceConfig.from_dict({
        "id": "arbeitnow-sponsored", "kind": "arbeitnow", "name": "Arbeitnow",
        "visa_sponsorship": True, "max_pages": 2,
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await ArbeitnowCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    assert batch.requests_made == 1
    assert batch.jobs[0].metadata["signals"]["visa_sponsorship"] is True


@pytest.mark.asyncio
async def test_full_board_collector_keeps_current_jobs_without_provider_timestamps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": "1", "title": "Backend Engineer", "location": "Berlin",
            "description": "Build systems.", "jobUrl": "https://example.test/1",
        }]})

    config = SourceConfig.from_dict({"id": "ashby", "kind": "ashby", "name": "Example", "slug": "example"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await AshbyCollector(config, client).collect(datetime.now(UTC))

    assert [job.source_job_id for job in jobs] == ["1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collector_type", "payload"),
    [
        (GreenhouseCollector, {"unexpected": []}),
        (AshbyCollector, {"unexpected": []}),
    ],
)
async def test_complete_json_collectors_reject_missing_job_arrays(collector_type, payload) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    config = SourceConfig.from_dict(
        {"id": "board", "kind": "greenhouse", "name": "Example", "slug": "example"}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="invalid"):
            await collector_type(config, client).collect(datetime.now(UTC))


@pytest.mark.asyncio
async def test_himalayas_placeholder_company_name_falls_back_to_company_slug() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "id": "1", "title": "Backend Engineer", "companyName": "name", "companySlug": "actual-company",
            "location": "Worldwide", "description": "Build systems.", "url": "https://example.test/1",
            "publishedAt": datetime.now(UTC).isoformat(),
        }]})

    config = SourceConfig(id="himalayas", kind="himalayas", name="Himalayas")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await HimalayasCollector(config, client).collect(datetime.now(UTC) - timedelta(days=1))

    assert jobs[0].company == "Actual Company"


@pytest.mark.asyncio
async def test_missing_remote_geography_remains_unknown_not_worldwide() -> None:
    def remote_ok_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{}, {
            "id": "1", "position": "Backend Engineer", "company": "Example",
            "description": "Build systems", "url": "https://example.test/1",
        }])

    config = SourceConfig.from_dict({"id": "remoteok", "kind": "remoteok", "name": "Remote OK"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote_ok_handler)) as client:
        jobs = await RemoteOkCollector(config, client).collect(datetime(2020, 1, 1, tzinfo=UTC))

    assert jobs[0].location == ""
    assert jobs[0].remote_scope == ""


@pytest.mark.asyncio
async def test_personio_collector_maps_the_public_xml_feed() -> None:
    xml = """
    <workzag-jobs><position><id>42</id><office>Munich</office><additionalOffices><office>Remote</office></additionalOffices>
    <department>Engineering</department><recruitingCategory>Permanent Employee</recruitingCategory>
    <name>Backend Engineer</name><jobDescriptions><jobDescription><name>About the role</name>
    <value>&lt;p&gt;Build reliable APIs.&lt;/p&gt;</value></jobDescription></jobDescriptions>
    <createdAt>2026-08-15T10:00:00+00:00</createdAt></position></workzag-jobs>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/xml"
        return httpx.Response(200, content=xml)

    config = SourceConfig.from_dict({
        "id": "personio", "kind": "personio", "name": "Personio", "company": "Personio",
        "slug": "open", "host": "https://open.jobs.personio.com",
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await PersonioCollector(config, client).collect(datetime.now(UTC))

    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].location == "Munich, Remote"
    assert "About the role" in jobs[0].description
    assert jobs[0].url == "https://open.jobs.personio.com/job/42?display=en"


def test_repair_text_fixes_common_job_description_mojibake() -> None:
    assert repair_text("Lemon.io â€” work that fits") == "Lemon.io — work that fits"


def test_plain_text_preserves_job_description_structure_without_scripts() -> None:
    value = """
    <h2>Responsibilities</h2>
    <p>Build reliable APIs.</p>
    <ul><li>Own backend services</li><li>Review designs</li></ul>
    <script>ignore me</script>
    """

    assert plain_text(value) == (
        "Responsibilities\nBuild reliable APIs.\n• Own backend services\n• Review designs"
    )


def test_plain_text_drops_page_chrome_from_full_html_documents() -> None:
    value = """
    <html><head><title>Trusted open source | Canonical</title></head><body>
    <a href="#main">Skip to main content</a>
    <nav><ul><li>Products</li></ul></nav>
    <div aria-hidden="true"><p>Your submission was sent successfully!</p><button>Close</button></div>
    <main><p>Canonical publishes Ubuntu.</p></main>
    <footer><p>All rights reserved.</p></footer>
    </body></html>
    """

    text = plain_text(value)

    assert "Canonical publishes Ubuntu." in text
    for chrome in ("Trusted open source", "Products", "submission was sent", "Close", "rights reserved"):
        assert chrome not in text


def test_plain_text_drops_popups_and_banners_hidden_by_css_class_rather_than_markup() -> None:
    value = """
    <body>
    <a href="#main-content" class="u-off-screen">Skip to main content</a>
    <div id="newsletter-signup" class="p-popup-notification"><p>Thank you for signing up for our
    newsletter! In these regular emails you will find the latest updates from Canonical.</p></div>
    <div class="cookie-policy"><p>We use cookies to improve your experience.</p></div>
    <div role="navigation"><p>Products</p></div>
    <main><p>Canonical publishes Ubuntu.</p></main>
    </body>
    """

    text = plain_text(value)

    assert "Canonical publishes Ubuntu." in text
    for chrome in ("Skip to main", "regular emails", "cookies", "Products"):
        assert chrome not in text


def test_plain_text_keeps_content_after_an_unclosed_option_inside_a_skipped_select() -> None:
    value = "<select><option>A<option>B</select><p>Real content.</p>"

    assert plain_text(value) == "Real content."


def test_description_blocks_create_safe_headings_lists_and_readable_paragraphs() -> None:
    value = (
        "About the role: Build reliable systems. "
        "Responsibilities:\n• Own backend services\n• Review designs\n\n"
        "What we offer: Remote work â€” worldwide."
    )

    blocks = description_blocks(value)

    assert blocks == [
        {"kind": "heading", "text": "About the role"},
        {"kind": "paragraph", "text": "Build reliable systems."},
        {"kind": "heading", "text": "Responsibilities"},
        {"kind": "list", "items": ["Own backend services", "Review designs"]},
        {"kind": "heading", "text": "What we offer"},
        {"kind": "paragraph", "text": "Remote work — worldwide."},
    ]


def test_description_blocks_render_only_a_safe_markdown_subset() -> None:
    blocks = description_blocks(
        "About the role\n\nUse **Python** with *care*; read [the guide](https://example.com/guide)."
    )

    paragraph = blocks[1]
    assert paragraph["segments"] == [
        {"kind": "text", "text": "Use "},
        {"kind": "strong", "text": "Python"},
        {"kind": "text", "text": " with "},
        {"kind": "emphasis", "text": "care"},
        {"kind": "text", "text": "; read "},
        {"kind": "link", "text": "the guide", "url": "https://example.com/guide"},
        {"kind": "text", "text": "."},
    ]
    assert "unsafe" not in plain_text("Safe<script>alert('unsafe')</script>")


@pytest.mark.asyncio
async def test_remote_collectors_preserve_location_and_attribution() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "jobicy" in request.url.host:
            return httpx.Response(200, json={"jobCount": 1, "jobs": [{
                "id": 1, "jobTitle": "Platform Engineer", "companyName": "Example",
                "jobGeo": "Europe", "jobDescription": "Cloud systems", "url": "https://example.test/j",
                "pubDate": datetime.now(UTC).isoformat(),
            }]})
        return httpx.Response(200, json={"jobs": [{
            "id": 2, "title": "Software Engineer", "company_name": "Remote Co",
            "candidate_required_location": "Worldwide", "description": "Backend",
            "url": "https://example.test/r", "publication_date": datetime.now(UTC).isoformat(),
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobicy = await JobicyCollector(SourceConfig("jobicy", "jobicy", "Jobicy"), client).collect(datetime.now(UTC) - timedelta(days=1))
        remotive = await RemotiveCollector(SourceConfig("remotive", "remotive", "Remotive"), client).collect(datetime.now(UTC) - timedelta(days=1))

    assert jobicy.jobs[0].remote_scope == "Europe"
    assert "Remotive" in remotive.attribution
    assert remotive.jobs[0].remote_scope == "Worldwide"


@pytest.mark.asyncio
async def test_smartrecruiters_detail_fetches_stay_in_listing_order() -> None:
    # Detail requests run concurrently (bounded); this asserts result order still matches the
    # listing order regardless of which detail response comes back first.
    detail_delay = {"1": 0.02, "2": 0.0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/postings"):
            return httpx.Response(200, json={
                "totalFound": 2,
                "content": [{"id": "1", "ref": "https://api.smartrecruiters.test/postings/1"},
                            {"id": "2", "ref": "https://api.smartrecruiters.test/postings/2"}],
            })
        posting_id = request.url.path.rsplit("/", 1)[-1]
        await asyncio.sleep(detail_delay[posting_id])
        return httpx.Response(200, json={
            "id": posting_id, "name": f"Engineer {posting_id}",
            "location": {"city": "Berlin", "country": "Germany"},
            "jobAd": {"sections": {"jobDescription": {"text": f"Role {posting_id}"}}},
        })

    config = SourceConfig.from_dict({"id": "sr", "kind": "smartrecruiters", "name": "Example", "slug": "example"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await SmartRecruitersCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30))

    assert [job.source_job_id for job in jobs] == ["1", "2"]
    assert jobs[0].description == "Role 1"
