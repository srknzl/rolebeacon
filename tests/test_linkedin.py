from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rolebeacon.collectors import (
    LINKEDIN_RESULT_CEILING,
    LinkedInCollector,
    linkedin_build_job,
    linkedin_parse_cards,
    linkedin_parse_cursor,
    linkedin_parse_description,
    linkedin_posting_url,
    linkedin_query_fingerprint,
    linkedin_search_params,
    linkedin_time_filter,
)
from rolebeacon.config import Settings
from rolebeacon.domain import SourceConfig
from rolebeacon.source_discovery import linkedin_source_candidates
from rolebeacon.sync import personalize_source

NOW = datetime.now(UTC)
POSTED_ON = (NOW - timedelta(days=3)).date().isoformat()
POSTED_YESTERDAY = (NOW - timedelta(days=1)).date().isoformat()

# Trimmed from a live response of LinkedIn's public seeMoreJobPostings endpoint, keeping the
# real class names, the urn that carries the job ID, and the ragged whitespace of the original.
# Dates are relative so the fixture does not age out of the collection window.
SEARCH_FRAGMENT = f"""
<li>
  <div class="base-card relative
    base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:4439500109">
    <a class="base-card__full-link" href="https://no.linkedin.com/jobs/view/backend-engineer-at-x-4439500109?refId=abc">
      <span class="sr-only">Backend Engineer</span>
    </a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">
        Backend Engineer
      </h3>
      <h4 class="base-search-card__subtitle">
        <a class="hidden-nested-link" href="https://www.linkedin.com/company/majestara">
          Majestara Development Limited
        </a>
      </h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">
          Time, Rogaland, Norway
        </span>
        <time class="job-search-card__listdate" datetime="{POSTED_ON}">3 days ago</time>
      </div>
    </div>
  </div>
</li>
<li>
  <div class="base-card base-search-card" data-entity-urn="urn:li:jobPosting:4349577471">
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Senior Platform Engineer</h3>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">Remote</span>
        <time class="job-search-card__listdate--new" datetime="{POSTED_YESTERDAY}">1 day ago</time>
      </div>
    </div>
  </div>
</li>
"""

POSTING_FRAGMENT = """
<div class="top-card-layout__entity-info">
  <h1 class="top-card-layout__title">Backend Engineer</h1>
  <span class="topcard__flavor">Majestara Development Limited</span>
</div>
<div class="description__text description__text--rich">
  <section class="show-more-less-html" data-max-lines="5">
    <div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5
        relative overflow-hidden">
      We build platforms.<br><br><strong>Requirements</strong><ul><li>Python &amp; Go</li><li>Kafka</li></ul>
    </div>
    <button class="show-more-less-html__button">Show more</button>
  </section>
</div>
"""


def source(**options) -> SourceConfig:
    return SourceConfig.from_dict(
        {"id": "linkedin-europe", "kind": "linkedin", "name": "LinkedIn — Europe",
         "keywords": "Backend Engineer", "location": "Europe", **options}
    )


def test_search_cards_parse_into_fields() -> None:
    cards = linkedin_parse_cards(SEARCH_FRAGMENT)

    assert [card["id"] for card in cards] == ["4439500109", "4349577471"]
    assert cards[0] == {
        "id": "4439500109",
        "title": "Backend Engineer",
        "company": "Majestara Development Limited",
        "location": "Time, Rogaland, Norway",
        "datetime": POSTED_ON,
    }
    # The screen-reader duplicate of the title inside the card link must not leak into any field.
    assert "Backend Engineer" not in cards[0]["company"]


def test_description_excludes_the_top_card() -> None:
    description = linkedin_parse_description(POSTING_FRAGMENT)

    assert description.startswith("We build platforms.")
    assert "Requirements" in description
    assert "• Python & Go" in description
    # The top card repeats title/company/location, which would otherwise be scored as if the
    # employer had written it. "Show more" is a button, which plain_text() already drops.
    assert "top-card" not in description and "Show more" not in description


def test_search_params_carry_keywords_location_recency_and_remote() -> None:
    since = datetime.now(UTC) - timedelta(hours=6)

    params = linkedin_search_params(source(), since, 40)
    assert params["keywords"] == "Backend Engineer"
    assert params["location"] == "Europe"
    assert params["start"] == 40
    assert params["f_TPR"].startswith("r21")  # ~6 hours in seconds
    assert "f_WT" not in params

    assert linkedin_search_params(source(remote=True), since, 0)["f_WT"] == "2"


def test_time_filter_never_drops_below_linkedins_hour_minimum() -> None:
    now = datetime.now(UTC)
    assert linkedin_time_filter(now - timedelta(seconds=30), now) == "r3600"
    assert linkedin_time_filter(now - timedelta(days=30), now) == "r2592000"


def test_job_uses_the_id_derived_canonical_url() -> None:
    card = linkedin_parse_cards(SEARCH_FRAGMENT)[0]

    job = linkedin_build_job(source(), card, "We build platforms.")

    assert job is not None
    # Not the country-specific, tracking-parameter URL from the card: two sources returning the
    # same posting must produce the same URL or deduplication cannot match them.
    assert job.url == "https://www.linkedin.com/jobs/view/4439500109/" == linkedin_posting_url("4439500109")
    assert (job.company, job.location) == ("Majestara Development Limited", "Time, Rogaland, Norway")
    assert job.published_at == datetime.fromisoformat(POSTED_ON).replace(tzinfo=UTC)


def test_posting_without_an_employer_is_skipped_rather_than_raising() -> None:
    card = linkedin_parse_cards(SEARCH_FRAGMENT)[1]

    assert "company" not in card
    assert linkedin_build_job(source(), card, "text") is None


def test_remote_source_marks_scope_even_when_the_card_names_a_city() -> None:
    card = linkedin_parse_cards(SEARCH_FRAGMENT)[0]

    assert linkedin_build_job(source(), card, "t").remote_scope == ""
    assert linkedin_build_job(source(remote=True), card, "t").remote_scope == "Time, Rogaland, Norway"


def test_matching_fingerprint_resumes_at_the_saved_offset() -> None:
    fingerprint = linkedin_query_fingerprint(source())

    assert linkedin_parse_cursor(f"{fingerprint}:340", fingerprint) == 340


def test_changed_query_starts_from_scratch() -> None:
    fingerprint = linkedin_query_fingerprint(source())
    changed = linkedin_query_fingerprint(source(keywords="Data Engineer"))

    assert changed != fingerprint
    assert linkedin_parse_cursor(f"{fingerprint}:340", changed) == 0


def test_unparseable_or_absent_cursor_starts_from_scratch() -> None:
    fingerprint = linkedin_query_fingerprint(source())

    for value in ("", "340", f"{fingerprint}:not-a-number", "garbage"):
        assert linkedin_parse_cursor(value, fingerprint) == 0


def test_offset_past_linkedins_ceiling_starts_from_scratch() -> None:
    fingerprint = linkedin_query_fingerprint(source())

    assert linkedin_parse_cursor(f"{fingerprint}:{LINKEDIN_RESULT_CEILING}", fingerprint) == 0


def test_recency_window_survives_a_changed_sync_interval() -> None:
    """f_TPR is derived from `since` and so differs every run; it must not invalidate the cursor."""
    assert linkedin_query_fingerprint(source()) == linkedin_query_fingerprint(source())


def _transport(pages: dict[int, str], posting: str = POSTING_FRAGMENT, on_posting=None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in request.url.path:
            start = int(request.url.params.get("start", 0))
            return httpx.Response(200, text=pages.get(start, ""))
        if on_posting is not None:
            on_posting()
        return httpx.Response(200, text=posting)

    return httpx.MockTransport(handler)


async def _collect(transport: httpx.MockTransport, config: SourceConfig, cursor: str = ""):
    async with httpx.AsyncClient(transport=transport) as client:
        return await LinkedInCollector(config, client).collect(datetime.now(UTC) - timedelta(days=30), cursor)


@pytest.fixture(autouse=True)
def instant_pacing(monkeypatch) -> None:
    """Run the real pacing logic without waiting for it."""
    from rolebeacon import collectors

    async def immediately(seconds: float) -> None:
        return None

    monkeypatch.setattr(collectors, "_linkedin_pause", immediately)
    # The request pace is process-wide state that rate limits widen, so start every test from the
    # same rhythm rather than from whatever the previous one left behind.
    monkeypatch.setattr(collectors, "_linkedin_ready_at", 0.0)
    monkeypatch.setattr(collectors, "_linkedin_pace_scale", 1.0)


async def test_collect_walks_pages_until_results_run_out() -> None:
    batch = await _collect(_transport({0: SEARCH_FRAGMENT, 2: SEARCH_FRAGMENT}), source())

    # Two cards per page, the second of which has no employer and is skipped.
    assert [job.source_job_id for job in batch.jobs] == ["4439500109", "4439500109"]
    assert batch.complete_snapshot is False  # a keyword search is not a board snapshot
    assert batch.truncated is False
    assert batch.cursor == ""  # exhausted, so the next run starts at the top of a fresh window


async def test_collect_resumes_from_a_saved_offset() -> None:
    config = source()
    fingerprint = linkedin_query_fingerprint(config)
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in request.url.path:
            requested.append(int(request.url.params.get("start", 0)))
            return httpx.Response(200, text="")
        return httpx.Response(200, text=POSTING_FRAGMENT)

    await _collect(httpx.MockTransport(handler), config, cursor=f"{fingerprint}:340")

    # The saved offset, then the two retries an empty page always gets before it is believed.
    assert requested == [340, 340, 340]


async def test_cancelling_checkpoints_the_jobs_already_collected() -> None:
    config = source()
    reads = 0

    def on_posting() -> None:
        nonlocal reads
        reads += 1
        if reads == 2:  # part-way through the second page
            raise asyncio.CancelledError

    batch = await _collect(_transport({0: SEARCH_FRAGMENT, 2: SEARCH_FRAGMENT}, on_posting=on_posting), config)

    assert [job.source_job_id for job in batch.jobs] == ["4439500109"]
    assert batch.truncated is True
    # Page one is finished with, and the cancel landed on the first posting of page two - which
    # was never read, so the walk has to resume at it rather than past it.
    assert batch.cursor == f"{linkedin_query_fingerprint(config)}:2"
    assert linkedin_parse_cursor(batch.cursor, linkedin_query_fingerprint(config)) == 2


async def test_repeated_rate_limiting_checkpoints_instead_of_failing() -> None:
    config = source()

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in request.url.path:
            return httpx.Response(200, text=SEARCH_FRAGMENT)
        return httpx.Response(429)

    batch = await _collect(httpx.MockTransport(handler), config)

    assert batch.jobs == []
    assert batch.truncated is True
    assert batch.cursor.startswith(f"{linkedin_query_fingerprint(config)}:")


async def test_linkedins_result_ceiling_ends_the_walk() -> None:
    config = source()
    fingerprint = linkedin_query_fingerprint(config)

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in request.url.path:
            # The live endpoint answers 400 rather than an empty page past the ceiling.
            return httpx.Response(400)
        return httpx.Response(200, text=POSTING_FRAGMENT)

    batch = await _collect(httpx.MockTransport(handler), config, cursor=f"{fingerprint}:990")

    assert batch.truncated is False and batch.cursor == ""


async def test_progress_is_reported_at_least_once_a_minute(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="rolebeacon.collectors"):
        await _collect(_transport({0: SEARCH_FRAGMENT}), source())

    messages = [record.getMessage() for record in caplog.records]
    assert any("starting from scratch" in message for message in messages)
    assert any("now reading" in message and "Backend Engineer" in message for message in messages)
    assert any("no more results" in message for message in messages)


async def test_resume_reports_how_far_back_it_is_collecting(caplog) -> None:
    config = source()
    fingerprint = linkedin_query_fingerprint(config)

    async with httpx.AsyncClient(transport=_transport({})) as client:
        with caplog.at_level(logging.INFO, logger="rolebeacon.collectors"):
            await LinkedInCollector(config, client).collect(
                datetime.now(UTC) - timedelta(hours=6), f"{fingerprint}:940"
            )

    resume = next(message for message in (r.getMessage() for r in caplog.records) if "continuing" in message)
    assert "posting 940" in resume
    assert "6h 00m" in resume


def test_long_breaks_are_jittered_rather_than_a_fixed_rhythm() -> None:
    from rolebeacon.collectors import LINKEDIN_BREAK_AFTER_RANGE, LINKEDIN_BREAK_DURATION_RANGE

    random.seed(0)
    stretches = [random.randint(*LINKEDIN_BREAK_AFTER_RANGE) for _ in range(20)]
    breaks = [random.uniform(*LINKEDIN_BREAK_DURATION_RANGE) for _ in range(20)]

    assert len(set(stretches)) > 1 and all(450 <= value <= 550 for value in stretches)
    assert len(set(breaks)) > 1 and all(240 <= value <= 300 for value in breaks)


def test_target_roles_reach_linkedin_without_a_dedicated_mechanism() -> None:
    personalized = personalize_source(
        source(keywords="stale"), {"target_roles": ["Backend Engineer", "Platform Engineer"]}
    )

    assert personalized.options["keywords"] == "Backend Engineer OR Platform Engineer"


def test_generated_sources_do_not_expand_a_continent_into_member_countries() -> None:
    sources = linkedin_source_candidates(
        [{"name": "Türkiye"}, {"name": "Europe"}, {"name": "North America"}, {"name": "Europe"}]
    )

    public = [item for item in sources if item.kind == "linkedin"]
    signed_in = [item for item in sources if item.kind == "linkedin_browser"]

    for rows in (public, signed_in):
        assert [item.options.get("location") for item in rows] == ["Türkiye", "Europe", "North America", ""]
        assert rows[-1].options["remote"] is True
    # Both paths ship switched off, so neither reaches LinkedIn until it is chosen.
    assert not any(item.enabled for item in sources)
    # Keywords are injected per sync by personalize_source, never baked into a generated row.
    assert all("keywords" not in item.options for item in sources)


def test_generated_sources_stay_distinct_when_saved(tmp_path, monkeypatch) -> None:
    """Every location keeps its own row: save_sources() must not collapse them onto one another."""
    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(tmp_path))
    settings = Settings.load()
    candidates = linkedin_source_candidates([{"name": "Türkiye"}, {"name": "Europe"}, {"name": "North America"}])

    settings.save_sources(candidates)
    settings.save_sources(candidates)  # a second setup save must update in place, not duplicate

    # Both kinds: neither carries a board slug or tenant, so both need URL identity to stay apart.
    for kind in ("linkedin", "linkedin_browser"):
        saved = [item for item in settings.load_sources() if item.kind == kind]
        assert sorted(item.options.get("location", "") for item in saved) == [
            "", "Europe", "North America", "Türkiye"
        ]


async def test_a_transient_server_error_is_retried_rather_than_failing_the_run() -> None:
    """LinkedIn's guest search answers HTTP 500 at random; one blip must not end the walk."""
    searches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if "seeMoreJobPostings" in request.url.path:
            searches += 1
            if searches == 1:
                return httpx.Response(500)
            return httpx.Response(200, text=SEARCH_FRAGMENT if searches == 2 else "")
        return httpx.Response(200, text=POSTING_FRAGMENT)

    batch = await _collect(httpx.MockTransport(handler), source())

    assert [job.source_job_id for job in batch.jobs] == ["4439500109"]
    assert batch.truncated is False


async def test_a_persistent_server_error_checkpoints_instead_of_failing() -> None:
    config = source()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    batch = await _collect(httpx.MockTransport(handler), config)

    assert batch.jobs == []
    assert batch.truncated is True
    assert linkedin_parse_cursor(batch.cursor, linkedin_query_fingerprint(config)) == 0


async def test_every_linkedin_source_shares_one_request_pace(monkeypatch) -> None:
    """Concurrent location rows must not each pace themselves into a combined 429 storm."""
    from rolebeacon import collectors

    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(collectors, "_linkedin_pause", record)
    monkeypatch.setattr(collectors, "_linkedin_ready_at", 0.0)

    await collectors._linkedin_gate()  # nothing outstanding, so this one goes straight through
    await collectors._linkedin_gate()  # a second source waits out the first request's spacing

    assert len(waits) == 1
    assert collectors.LINKEDIN_POSTING_DELAY_RANGE[0] <= waits[0] <= collectors.LINKEDIN_POSTING_DELAY_RANGE[1]


async def test_a_rate_limit_slows_every_later_request(monkeypatch) -> None:
    """The pace LinkedIn allows is discovered, not guessed: each 429 widens the shared spacing."""
    from rolebeacon import collectors

    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(collectors, "_linkedin_pause", record)
    monkeypatch.setattr(collectors, "_linkedin_ready_at", 0.0)
    monkeypatch.setattr(collectors, "_linkedin_pace_scale", 1.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in request.url.path:
            return httpx.Response(200, text=SEARCH_FRAGMENT if not waits else "")
        return httpx.Response(429)

    await _collect(httpx.MockTransport(handler), source())

    assert collectors._linkedin_pace_scale > 1.0
    # The widened spacing is what the next request actually waits, not just a recorded number.
    assert max(waits) > collectors.LINKEDIN_POSTING_DELAY_RANGE[1]


def test_widening_stops_at_a_ceiling(monkeypatch) -> None:
    from rolebeacon import collectors

    monkeypatch.setattr(collectors, "_linkedin_pace_scale", 1.0)
    for _ in range(50):
        collectors.linkedin_widen_pace()

    slowest = collectors.LINKEDIN_POSTING_DELAY_RANGE[1] * collectors._linkedin_pace_scale
    assert slowest == pytest.approx(collectors.LINKEDIN_PACE_CEILING_SECONDS)


class _FakeContext:
    """The one context call the collector makes: does a session cookie exist for linkedin.com?"""

    def __init__(self, signed_in: bool):
        self.signed_in = signed_in

    async def cookies(self, _url: str) -> list[dict[str, str]]:
        return [{"name": "li_at", "value": "irrelevant"}] if self.signed_in else [{"name": "bcookie"}]


class _FakePage:
    """Enough of a Playwright page to read descriptions without a browser."""

    def __init__(self, description: str = "<p>Build the platform.</p>", signed_in: bool = True):
        self.description = description
        self.context = _FakeContext(signed_in)
        self.visited: list[str] = []
        self.waited: list[str] = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.visited.append(url)

    async def wait_for_function(self, script: str, **_kwargs) -> None:
        self.waited.append(script)

    async def evaluate(self, _script: str) -> dict[str, str]:
        return {"via": "JobDetails_AboutTheJob" if self.description else "", "html": self.description}


def _browser_source(**options) -> SourceConfig:
    values = {"keywords": "Backend Engineer", "location": "Europe", **options}
    return SourceConfig.from_dict({"id": "linkedin-browser-europe", "kind": "linkedin_browser",
                                   "name": "LinkedIn (signed in) — Europe", **values})


def _cards_then_nothing(request: httpx.Request) -> httpx.Response:
    """The guest search still supplies the cards; only the description comes from the browser."""
    if "seeMoreJobPostings" not in request.url.path:
        return httpx.Response(404)
    return httpx.Response(200, text=SEARCH_FRAGMENT if request.url.params.get("start") == "0" else "")


async def _browser_collect(page, config: SourceConfig, cursor: str = "", monkeypatch=None,
                           handler=_cards_then_nothing):
    from contextlib import asynccontextmanager

    from rolebeacon import collectors

    @asynccontextmanager
    async def fake_browser(_progress):
        yield page

    monkeypatch.setattr(collectors, "_linkedin_browser", fake_browser)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = collectors.LinkedInBrowserCollector(config, client)
        return await collector.collect(datetime.now(UTC) - timedelta(days=30), cursor)


async def test_the_signed_in_walk_reads_descriptions_from_the_page_and_the_rest_from_the_cards(monkeypatch) -> None:
    """LinkedIn renders its own chrome in the account's language; the public cards stay English."""
    page = _FakePage()

    batch = await _browser_collect(page, _browser_source(), monkeypatch=monkeypatch)

    # The second card has no employer, so it is skipped before the browser is ever asked for it.
    assert [job.source_job_id for job in batch.jobs] == ["4439500109"]
    assert batch.jobs[0].description == "Build the platform."
    assert (batch.jobs[0].company, batch.jobs[0].location) == (
        "Majestara Development Limited", "Time, Rogaland, Norway")
    assert batch.jobs[0].published_at == datetime.fromisoformat(POSTED_ON).replace(tzinfo=UTC)
    # The same canonical URL the public collector produces, so the two paths deduplicate.
    assert batch.jobs[0].url == linkedin_posting_url("4439500109")
    assert page.visited == [linkedin_posting_url("4439500109")]
    assert batch.complete_snapshot is False
    assert batch.cursor == ""


async def test_a_browser_that_will_not_close_does_not_hold_the_run_open(monkeypatch) -> None:
    """A stop has to stop: a cancelled walk once left the driver waiting on a browser long gone."""
    from rolebeacon import collectors

    class Stuck:
        def __init__(self):
            self.asked = False

        async def close(self) -> None:
            self.asked = True
            await asyncio.sleep(3600)

        stop = close

    context, playwright = Stuck(), Stuck()
    monkeypatch.setattr(collectors, "LINKEDIN_BROWSER_CLOSE_SECONDS", 0.01)

    await asyncio.wait_for(collectors._linkedin_close(context, playwright), 5)

    assert context.asked and playwright.asked


async def test_the_signed_in_walk_waits_for_the_description_to_render(monkeypatch) -> None:
    """The signed-in page hydrates: read it too early and every posting arrives as an empty shell."""
    from rolebeacon import collectors

    page = _FakePage()

    await _browser_collect(page, _browser_source(), monkeypatch=monkeypatch)

    # Waits for text, not just the container: LinkedIn renders the heading a moment before the
    # posting, and reading on the element's arrival collects an empty shell.
    assert page.waited == [collectors.LINKEDIN_FILLED_SCRIPT]


async def test_a_description_that_never_renders_is_skipped_rather_than_failing(monkeypatch, caplog) -> None:
    """A wait that times out is not an error - the posting is skipped and the walk carries on."""

    class SlowPage(_FakePage):
        async def wait_for_function(self, script: str, **kwargs) -> None:
            raise TimeoutError("Timeout waiting for the description to render")

    page = SlowPage(description="")

    with caplog.at_level(logging.WARNING, logger="rolebeacon.collectors"):
        batch = await _browser_collect(page, _browser_source(), monkeypatch=monkeypatch)

    assert batch.jobs == []
    assert batch.truncated is False  # walked to the end rather than checkpointing on the timeout
    assert "returned no description" in caplog.text


async def test_the_signed_in_walk_waits_for_a_sign_in(monkeypatch, caplog) -> None:
    from rolebeacon import collectors

    class SignsInWhileAsked(_FakeContext):
        """Signed out when the walk first looks, signed in by the time it looks again."""

        def __init__(self):
            super().__init__(signed_in=False)
            self.asked = 0

        async def cookies(self, url: str) -> list[dict[str, str]]:
            self.asked += 1
            self.signed_in = self.asked > 1
            return await super().cookies(url)

    page = _FakePage(signed_in=False)
    page.context = SignsInWhileAsked()

    with caplog.at_level(logging.INFO, logger="rolebeacon.collectors"):
        batch = await _browser_collect(page, _browser_source(), monkeypatch=monkeypatch)

    assert "waiting for you to sign in" in caplog.text
    assert "signed in, continuing" in caplog.text
    # The sign-in page was offered before any posting was opened.
    assert page.visited == [collectors.LINKEDIN_LOGIN_URL, linkedin_posting_url("4439500109")]
    assert len(batch.jobs) == 1


async def test_a_guest_page_is_not_mistaken_for_a_signed_in_one(monkeypatch, caplog) -> None:
    """LinkedIn serves guests the same URLs a member sees, so the URL cannot answer this."""
    from rolebeacon import collectors

    page = _FakePage(signed_in=False)

    monkeypatch.setattr(collectors, "LINKEDIN_LOGIN_TIMEOUT_SECONDS", 0.0)
    with caplog.at_level(logging.INFO, logger="rolebeacon.collectors"):
        batch = await _browser_collect(page, _browser_source(), monkeypatch=monkeypatch)

    assert "waiting for you to sign in" in caplog.text
    assert batch.jobs == []
    # No posting was opened: a guest walk through the browser collects nothing the public
    # collector could not already reach, and looks like a window refreshing itself.
    assert not any("jobs/view" in url for url in page.visited)


async def test_closing_the_window_checkpoints_the_walk(monkeypatch) -> None:
    config = _browser_source()

    class ClosedPage(_FakePage):
        async def goto(self, url: str, **kwargs) -> None:
            raise RuntimeError("Target page, context or browser has been closed")

    batch = await _browser_collect(ClosedPage(), config, monkeypatch=monkeypatch)

    assert batch.truncated is True
    # The posting it never finished, not the one after it.
    assert linkedin_parse_cursor(batch.cursor, linkedin_query_fingerprint(config)) == 0


async def test_one_empty_page_does_not_end_a_walk() -> None:
    """LinkedIn serves an empty page under load; believing the first one cost a real run 60% of it."""
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" not in request.url.path:
            return httpx.Response(200, text=POSTING_FRAGMENT)
        start = int(request.url.params.get("start", 0))
        asked.append(start)
        if start == 2 and asked.count(2) == 1:
            return httpx.Response(200, text="")  # the empty page LinkedIn serves when it wants a rest
        return httpx.Response(200, text=SEARCH_FRAGMENT if start in (0, 2) else "")

    batch = await _collect(httpx.MockTransport(handler), source())

    assert asked.count(2) == 2  # the empty offset was asked again rather than accepted as the end
    assert len(batch.jobs) == 2  # one usable posting from each of the two real pages


async def test_a_search_that_stays_empty_is_accepted_as_finished(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in request.url.path:
            return httpx.Response(200, text=SEARCH_FRAGMENT if not int(request.url.params.get("start", 0)) else "")
        return httpx.Response(200, text=POSTING_FRAGMENT)

    with caplog.at_level(logging.INFO, logger="rolebeacon.collectors"):
        batch = await _collect(httpx.MockTransport(handler), source())

    assert "no more results" in caplog.text
    assert batch.cursor == ""  # exhausted, so the next run starts at the top of a fresh window
    assert batch.truncated is False
