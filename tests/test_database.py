from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from rolebeacon.database import Database, JobFilters, canonicalize_url, company_key
from rolebeacon.domain import CollectedJob, EligibilityResult, EligibilityStatus, JobStatus, ScoreResult


def sample_job(source: str = "source-a", description: str = "Java backend role") -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id="job-1",
        title="Senior Backend Engineer",
        company="Example",
        location="Remote Worldwide",
        description=description,
        url="https://example.com/jobs/1?utm_source=test",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def test_canonicalize_url_removes_tracking_parameters() -> None:
    assert canonicalize_url("HTTPS://Example.com/jobs/1/?utm_source=x&team=core#apply") == "https://example.com/jobs/1?team=core"


def test_upsert_deduplicates_sources_and_marks_content_changes(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()

    first_id, first_changed = database.upsert_job(sample_job())
    duplicate = sample_job(source="source-b")
    duplicate.source_job_id = "other-id"
    second_id, second_changed = database.upsert_job(duplicate)
    _, unchanged = database.upsert_job(sample_job())
    _, changed = database.upsert_job(sample_job(description="Updated Java and Go backend role"))

    assert first_changed is True
    assert second_changed is False
    assert first_id == second_id
    assert unchanged is False
    assert changed is True
    assert database.get_job(first_id)["description"] == "Updated Java and Go backend role"


def test_full_text_search_returns_matching_job(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    database.upsert_job(sample_job(description="Built distributed systems with Kafka"))

    jobs = database.list_jobs(JobFilters(query="Kafka"))

    assert len(jobs) == 1
    assert jobs[0]["company"] == "Example"


def test_scoring_version_change_requeues_existing_job(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    job_id, _ = database.upsert_job(sample_job())
    database.save_evaluation(
        job_id,
        EligibilityResult(
            status=EligibilityStatus.ELIGIBLE,
            route="remote-from-tr",
            sponsorship="unknown",
            relocation="unknown",
            location_fit="worldwide",
            reasons=[],
            risks=[],
        ),
        ScoreResult(
            total=70,
            dimensions={
                "role_domain": 20,
                "stack": 15,
                "domain_experience": 15,
                "seniority": 8,
                "location_authorization": 10,
                "salary_employment": 2,
            },
            confidence=0.7,
            verdict="review",
            evidence=[],
            gaps=[],
            provider="rules",
            model="test",
            prompt_version="job-fit-v1:rules",
        ),
        "scored",
    )

    assert database.pending_job_ids("job-fit-v1:rules") == []
    assert database.pending_job_ids("job-fit-v2:rules") == [job_id]


def test_higher_trust_source_controls_canonical_content(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    trusted = sample_job(source="official", description="Full official description")
    job_id, _ = database.upsert_job(trusted, source_priority=90)
    aggregate = sample_job(source="aggregate", description="Short aggregate excerpt")
    aggregate.source_job_id = "aggregate-1"

    same_id, changed = database.upsert_job(aggregate, source_priority=40)

    assert same_id == job_id
    assert changed is False
    assert database.get_job(job_id)["description"] == "Full official description"


def test_probable_duplicate_can_be_reviewed_and_merged(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    first = sample_job()
    first.url = "https://example.com/jobs/backend-1"
    first_id, _ = database.upsert_job(first)
    second = sample_job(source="source-b")
    second.source_job_id = "job-2"
    second.title = "Senior Backend Engineeer"
    second.url = "https://board.test/positions/2"
    second_id, _ = database.upsert_job(second)

    candidates = database.list_duplicate_candidates()
    assert candidates
    winner = database.merge_duplicate(candidates[0]["id"], first_id)

    assert winner == first_id
    assert database.get_job(second_id)["merged_into_job_id"] == first_id
    assert all(job["id"] != second_id for job in database.list_jobs())


def test_api_budget_is_hard_limited(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()

    assert database.reserve_api_requests("provider", 2, 3) is True
    assert database.reserve_api_requests("provider", 2, 3) is False
    assert database.list_api_usage()[0]["request_count"] == 2


def test_v1_database_is_migrated_in_place(tmp_path) -> None:
    path = tmp_path / "v1.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL, company TEXT NOT NULL, company_key TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', apply_url TEXT NOT NULL DEFAULT '',
            remote_scope TEXT NOT NULL DEFAULT '', employment_type TEXT NOT NULL DEFAULT '', salary_min REAL,
            salary_max REAL, salary_currency TEXT NOT NULL DEFAULT '', published_at TEXT, updated_at TEXT,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, content_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',
            score_status TEXT NOT NULL DEFAULT 'pending', metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE job_sources (
            source_id TEXT NOT NULL, source_job_id TEXT NOT NULL, job_id INTEGER NOT NULL,
            source_url TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            PRIMARY KEY(source_id, source_job_id)
        );
        CREATE TABLE source_state (
            source_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'idle', last_started_at TEXT,
            last_successful_sync_at TEXT, last_error TEXT NOT NULL DEFAULT '', cursor TEXT NOT NULL DEFAULT '',
            jobs_seen INTEGER NOT NULL DEFAULT 0, jobs_changed INTEGER NOT NULL DEFAULT 0
        );
    """)
    connection.close()

    database = Database(path)
    database.initialize()

    with database.connect() as migrated:
        job_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(jobs)")}
        source_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(job_sources)")}
        version = migrated.execute("SELECT version FROM schema_migrations WHERE version = 2").fetchone()
    assert {"primary_source_id", "merged_into_job_id", "normalized_title"} <= job_columns
    assert {"source_priority", "metadata_json", "content_hash"} <= source_columns
    assert version["version"] == 2


def _score(
    database: Database,
    job_id: int,
    total: int,
    role_domain: int,
    stack: int,
    status: EligibilityStatus = EligibilityStatus.ELIGIBLE,
) -> None:
    database.save_evaluation(
        job_id,
        EligibilityResult(
            status=status,
            route="remote-from-tr",
            sponsorship="unknown",
            relocation="unknown",
            location_fit="worldwide",
            reasons=[],
            risks=[],
        ),
        ScoreResult(
            total=total,
            dimensions={
                "role_domain": role_domain,
                "stack": stack,
                "domain_experience": 0,
                "seniority": 0,
                "location_authorization": 0,
                "salary_employment": 0,
            },
            confidence=0.7,
            verdict="review",
            evidence=[],
            gaps=[],
            provider="rules",
            model="test",
            prompt_version="job-fit-v1:rules",
        ),
        "scored",
    )


def _two_scored_jobs(tmp_path) -> tuple[Database, int, int]:
    """A high-scoring generalist and a lower-scoring specialist, so each sort key orders them differently."""
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    generalist = sample_job()
    generalist_id, _ = database.upsert_job(generalist)
    specialist = sample_job(source="source-b")
    specialist.source_job_id = "job-2"
    specialist.title = "Staff Platform Engineer"
    specialist.url = "https://example.com/jobs/2"
    specialist_id, _ = database.upsert_job(specialist)
    _score(database, generalist_id, total=80, role_domain=10, stack=18)
    _score(database, specialist_id, total=60, role_domain=25, stack=4)
    return database, generalist_id, specialist_id


def test_each_sort_key_orders_the_same_result_set_differently(tmp_path) -> None:
    database, generalist_id, specialist_id = _two_scored_jobs(tmp_path)

    def order(sort: str) -> list[int]:
        return [job["id"] for job in database.list_jobs(sort=sort)]

    assert order("opportunity") == [generalist_id, specialist_id]
    assert order("job_fit") == [generalist_id, specialist_id]
    assert order("stack_match") == [generalist_id, specialist_id]
    assert order("title_match") == [specialist_id, generalist_id]


def test_an_unknown_sort_key_falls_back_instead_of_reaching_the_query(tmp_path) -> None:
    database, generalist_id, specialist_id = _two_scored_jobs(tmp_path)

    jobs = database.list_jobs(sort="total; DROP TABLE jobs")

    assert [job["id"] for job in jobs] == [generalist_id, specialist_id]


def test_decision_ready_sort_puts_eligible_before_unknown_before_ineligible(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    ids: dict[str, int] = {}
    for index, (label, status, score) in enumerate(
        (
            ("unknown", EligibilityStatus.UNKNOWN, 95),
            ("ineligible", EligibilityStatus.INELIGIBLE, 99),
            ("eligible", EligibilityStatus.ELIGIBLE, 60),
        ),
        start=1,
    ):
        job = sample_job(source=f"source-{index}")
        job.source_job_id = label
        job.title = f"{label.title()} Backend Engineer"
        job.url = f"https://example.com/jobs/{label}"
        ids[label], _ = database.upsert_job(job)
        _score(database, ids[label], total=score, role_domain=20, stack=10, status=status)

    assert [job["id"] for job in database.list_jobs()] == [
        ids["eligible"],
        ids["unknown"],
        ids["ineligible"],
    ]


def test_filters_narrow_the_set_and_the_count_agrees_with_the_page(tmp_path) -> None:
    database, _, specialist_id = _two_scored_jobs(tmp_path)
    filters = JobFilters(title="platform")

    assert database.count_jobs(filters) == 1
    assert [job["id"] for job in database.list_jobs(filters)] == [specialist_id]
    assert database.count_jobs(JobFilters(min_title_match=20)) == 1
    assert database.count_jobs(JobFilters()) == 2


def test_experience_requirements_are_stored_and_hide_unmet_experience_filters_them_out(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    clean = sample_job()
    clean_id, _ = database.upsert_job(clean)
    # Distinct company so the strong-identity dedup (company+title+location) does not fold this
    # into the same job row as `clean`.
    gapped = sample_job(source="source-b")
    gapped.company = "Other Example"
    gapped.source_job_id = "job-2"
    gapped.url = "https://example.com/jobs/2"
    gapped_id, _ = database.upsert_job(gapped)

    def eligibility() -> EligibilityResult:
        return EligibilityResult(
            status=EligibilityStatus.ELIGIBLE, route="remote-from-tr", sponsorship="unknown",
            relocation="unknown", location_fit="worldwide", reasons=[], risks=[],
        )

    def result(gaps: list[dict[str, str]]) -> ScoreResult:
        return ScoreResult(
            total=70,
            dimensions={
                "role_domain": 20, "stack": 15, "domain_experience": 15,
                "seniority": 8, "location_authorization": 10, "salary_employment": 2,
            },
            confidence=0.7, verdict="review", evidence=[], gaps=gaps,
            provider="rules", model="test", prompt_version="job-fit-v7:rules",
        )

    database.save_evaluation(
        clean_id, eligibility(), result([]), "scored", requirements=[{"skill": "Java", "years": 5, "unmet": False}]
    )
    database.save_evaluation(
        gapped_id,
        eligibility(),
        result([{"requirement": "Posting asks for 6+ years of Rust, not found in your profile", "severity": "medium"}]),
        "scored",
        requirements=[{"skill": "Rust", "years": 6, "unmet": True}],
    )

    assert database.get_job(clean_id)["requirements"] == [{"skill": "Java", "years": 5, "unmet": False}]
    assert database.get_job(gapped_id)["requirements"] == [{"skill": "Rust", "years": 6, "unmet": True}]
    assert [job["id"] for job in database.list_jobs(JobFilters(hide_unmet_experience=True))] == [clean_id]


def test_hide_unmet_experience_works_on_llm_scored_jobs_too(tmp_path) -> None:
    # The old filter matched literal "years of" text inside gaps_json, which only rules-mode gap
    # text ever contained - an LLM-scored job's differently-worded gap text made the filter a
    # silent no-op. requirements_json is populated the same deterministic way regardless of which
    # provider scored the job, so the filter must work here too.
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    job_id, _ = database.upsert_job(sample_job())
    eligibility = EligibilityResult(
        status=EligibilityStatus.ELIGIBLE, route="remote-from-tr", sponsorship="unknown",
        relocation="unknown", location_fit="worldwide", reasons=[], risks=[],
    )
    score = ScoreResult(
        total=70,
        dimensions={
            "role_domain": 20, "stack": 15, "domain_experience": 15,
            "seniority": 8, "location_authorization": 10, "salary_employment": 2,
        },
        confidence=0.7, verdict="review", evidence=[],
        gaps=[{"requirement": "Candidate has not shown Rust proficiency", "severity": "medium"}],
        provider="llm", model="test-model", prompt_version="job-fit-v11:test-model",
    )

    database.save_evaluation(job_id, eligibility, score, "scored", requirements=[{"skill": "Rust", "years": 6, "unmet": True}])

    assert database.list_jobs(JobFilters(hide_unmet_experience=True)) == []


def test_hide_mismatched_titles_excludes_low_role_domain_jobs_but_keeps_unscored_ones(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    matched = sample_job()
    matched_id, _ = database.upsert_job(matched)
    mismatched = sample_job(source="source-b")
    mismatched.company = "Other Example"
    mismatched.source_job_id = "job-2"
    mismatched.url = "https://example.com/jobs/2"
    mismatched_id, _ = database.upsert_job(mismatched)
    # A third, never-scored job - it must stay visible until scoring actually confirms a mismatch.
    unscored = sample_job(source="source-c")
    unscored.company = "Third Example"
    unscored.source_job_id = "job-3"
    unscored.url = "https://example.com/jobs/3"
    unscored_id, _ = database.upsert_job(unscored)

    def result(role_domain: int) -> ScoreResult:
        return ScoreResult(
            total=70,
            dimensions={
                "role_domain": role_domain, "stack": 15, "domain_experience": 15,
                "seniority": 8, "location_authorization": 10, "salary_employment": 2,
            },
            confidence=0.7, verdict="review", evidence=[], gaps=[],
            provider="rules", model="test", prompt_version="job-fit-v7:rules",
        )

    eligibility = EligibilityResult(
        status=EligibilityStatus.ELIGIBLE, route="remote-from-tr", sponsorship="unknown",
        relocation="unknown", location_fit="worldwide", reasons=[], risks=[],
    )
    database.save_evaluation(matched_id, eligibility, result(25), "scored")
    database.save_evaluation(mismatched_id, eligibility, result(2), "scored")

    kept = {job["id"] for job in database.list_jobs(JobFilters(hide_mismatched_titles=True))}
    assert kept == {matched_id, unscored_id}
    assert database.count_jobs(JobFilters(hide_mismatched_titles=True)) == 2


def test_source_ids_filter_matches_any_of_several_grouped_source_rows(tmp_path) -> None:
    # The Jobs page groups every Google/Amazon country row into one dropdown option, so the
    # filter must accept a set of ids and match a job from any one of them.
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    germany_id, _ = database.upsert_job(sample_job(source="google-careers-germany"))
    database.upsert_job(sample_job(source="adzuna-uk"))

    jobs = database.list_jobs(JobFilters(source_ids=("google-careers-germany", "google-careers-france")))

    assert [job["id"] for job in jobs] == [germany_id]


def test_starting_a_source_clears_its_stale_skip_reason_note(tmp_path) -> None:
    # A source skipped for "minimum sync interval" and then actually started later must not keep
    # showing that stale note next to a status the note no longer explains.
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    database.skip_source("google-careers-germany", "minimum_sync_interval")

    database.start_source("google-careers-germany")

    state = database.source_state("google-careers-germany")
    assert state is not None
    assert state["status"] == "running"
    assert state["last_skipped_reason"] == ""


def test_reset_stale_sync_runs_closes_interrupted_source_attempts(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    database.start_source("interrupted")
    run_id = database.start_sync_run("interrupted")

    assert database.reset_stale_sync_runs() == 1

    state = database.source_state("interrupted")
    assert state is not None
    assert state["status"] == "error"
    assert "interrupted" in state["last_error"].casefold()
    with database.connect() as connection:
        run = connection.execute("SELECT * FROM source_sync_runs WHERE id = ?", (run_id,)).fetchone()
    assert run is not None
    assert run["status"] == "error"
    assert run["finished_at"]


def test_company_in_filter_matches_priority_or_watchlist_companies(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    watched = CollectedJob(
        source="fixture", source_job_id="watched", title="Backend Engineer", company="Watched Co",
        location="Remote", description="Build things.", url="https://example.com/jobs/watched",
    )
    other = CollectedJob(
        source="fixture", source_job_id="other", title="Backend Engineer", company="Other Co",
        location="Remote", description="Build things.", url="https://example.com/jobs/other",
    )
    watched_id, _ = database.upsert_job(watched)
    database.upsert_job(other)

    jobs = database.list_jobs(JobFilters(company_in=(company_key("Watched Co"),)))

    assert [job["id"] for job in jobs] == [watched_id]


def test_regenerating_an_artifact_cannot_rewind_a_prepared_application(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    job_id, _ = database.upsert_job(sample_job())
    database.save_application(job_id, status="preparing", resume_path="resume.pdf")
    database.save_application(job_id, status="ready", packet_path="packet.json")

    database.save_application(job_id, status="preparing", resume_path="resume-2.pdf")

    application = database.list_applications()[0]
    assert application["status"] == "ready"
    assert application["resume_path"] == "resume-2.pdf"


def test_recording_an_outcome_puts_a_job_on_the_pipeline_board(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    job_id, _ = database.upsert_job(sample_job())

    database.save_feedback(job_id, JobStatus.APPLIED)

    # The pipeline board reads jobs.status directly now, so an outcome alone (no resume ever
    # generated) is enough to place the job on the board without touching the applications table.
    assert [job["id"] for job in database.list_jobs(JobFilters(status="applied"))] == [job_id]
    assert not database.list_applications()


def test_legacy_job_statuses_migrate_to_the_five_pipeline_states(tmp_path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    database = Database(db_path)
    database.initialize()
    legacy_to_new = {
        "interested": "bookmarked",
        "maybe": "bookmarked",
        "rejected": "not_interested",
        "applied": "applied",
        "interview": "applied",
        "offer": "offer",
    }
    job_ids: dict[str, int] = {}
    for index, legacy_status in enumerate(legacy_to_new):
        job = CollectedJob(
            # Distinct company per row so the strong-identity dedup (company+title+location)
            # does not fold these into a single job the way real duplicate postings would.
            source="legacy", source_job_id=str(index), title="Engineer", company=f"Example {index}",
            location="Remote", description="Role", url=f"https://example.com/jobs/legacy-{index}",
        )
        job_id, _ = database.upsert_job(job)
        job_ids[legacy_status] = job_id
    with sqlite3.connect(db_path) as connection:
        for legacy_status, job_id in job_ids.items():
            connection.execute("UPDATE jobs SET status = ? WHERE id = ?", (legacy_status, job_id))
        # This fresh database already recorded migration 3 as a no-op. Undo that marker to
        # simulate a real pre-migration database, then let initialize() run it for real.
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
        connection.commit()

    Database(db_path).initialize()

    for legacy_status, job_id in job_ids.items():
        assert database.get_job(job_id)["status"] == legacy_to_new[legacy_status]
