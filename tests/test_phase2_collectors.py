from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rolebeacon.collectors import (
    ArbeitnowCollector,
    JobicyCollector,
    RemotiveCollector,
    description_blocks,
    plain_text,
    repair_text,
    stable_alert_job_id,
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


def test_alert_ids_are_stable_across_email_messages() -> None:
    first = "https://www.linkedin.com/jobs/view/backend-engineer-123456/?trackingId=one"
    second = "https://linkedin.com/jobs/view/123456?trackingId=two"

    assert stable_alert_job_id(first) == stable_alert_job_id(second)
