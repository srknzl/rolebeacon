from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rolebeacon.collectors import plain_text
from rolebeacon.config import Settings
from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob
from rolebeacon.llm import LlmClient
from rolebeacon.sync import SyncService, deduplicate_source_jobs


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
