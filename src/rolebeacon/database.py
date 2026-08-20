from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .domain import CollectedJob, EligibilityResult, JobStatus, ScoreResult
from .scoring import seniority_title_pattern

# The score the Jobs page displays and sorts by: job fit weighted 80% with company fit 20%, and
# plain job fit until the job is eligible and its employer has actually been researched. Written
# once because the list, the single-job query, and the minimum-score filter have to mean the same
# number - they did not, so a job could be hidden by a threshold it cleared on the shown score.
OPPORTUNITY_SCORE_SQL = """CASE WHEN ms.total IS NULL THEN NULL
                    WHEN cs.total IS NULL OR e.status <> 'eligible' THEN ms.total
                    ELSE CAST(ROUND(ms.total * 0.8 + cs.total * 0.2) AS INTEGER) END"""

# The company joins the score needs, so the list and its total count can evaluate it alike.
COMPANY_SCORE_JOINS = """LEFT JOIN companies c ON c.normalized_name = j.company_key
            LEFT JOIN company_scores cs ON cs.id = (
                SELECT id FROM company_scores WHERE company_id = c.id ORDER BY created_at DESC, id DESC LIMIT 1
            )"""

# JobFilters.provider value meaning "any language model", as opposed to one named provider.
# The stored provider is "rules", "ollama", or "openai-compatible"; the UI offers engines, not
# endpoints, so this stands in for the whole model family.
MODEL_SCORED_PROVIDER_FILTER = "model"

# One SQL test per facet value, mirroring the clause _job_filters builds for that same value.
# test_database asserts every one of them agrees with count_jobs for the value on its own, which
# is what keeps the two definitions from drifting apart.
FACET_VALUE_SQL: dict[str, dict[str, str]] = {
    "eligibility": {
        value: f"COALESCE(e.status, 'unknown') = '{value}'" for value in ("eligible", "unknown", "ineligible")
    },
    "sponsorship": {
        value: f"COALESCE(e.sponsorship, 'unknown') = '{value}'" for value in ("available", "unavailable", "unknown")
    },
    "relocation": {
        value: f"COALESCE(e.relocation, 'unknown') = '{value}'"
        for value in ("available", "unavailable", "unknown")
    },
    "job_status": {
        value: f"j.status = '{value}'"
        for value in ("new", "bookmarked", "applied", "offer", "rejected", "not_interested")
    },
    "work_model": {
        "remote_worldwide": "j.location_bucket = 'remote:worldwide'",
        "remote": "j.location_bucket LIKE 'remote:%'",
        "onsite": "j.location_bucket NOT LIKE 'remote:%'",
    },
    "provider": {
        "rules": "COALESCE(ms.provider, '') = 'rules'",
        MODEL_SCORED_PROVIDER_FILTER: "COALESCE(ms.provider, '') NOT IN ('', 'rules')",
    },
}
# The JobFilters field each facet fills, so a facet can be counted with its own choices dropped.
FACET_FILTER_FIELDS: dict[str, str] = {
    "eligibility": "eligibility",
    "sponsorship": "sponsorship",
    "relocation": "relocation",
    "job_status": "status",
    "work_model": "work_model",
    "provider": "provider",
}

Choice = str | tuple[str, ...]


def _chosen(value: Choice) -> tuple[str, ...]:
    """The values a choice filter is asking for, empty when it is asking for nothing."""
    values = (value,) if isinstance(value, str) else tuple(value)
    return tuple(item for item in values if item)


@dataclass(slots=True)
class JobFilters:
    """Every way the job list can be narrowed. Filters narrow the set; `sort` only reorders it."""

    query: str = ""
    title: str = ""
    technologies: tuple[str, ...] = ()
    # Choice filters accept one value or several. Several are an OR within the facet and still an
    # AND across facets, which is what a person means by ticking two boxes in one menu. A plain
    # string is the same filter with one value, so existing callers read unchanged.
    route: Choice = ""
    status: Choice = ""
    source_ids: tuple[str, ...] = ()
    company: str = ""
    company_in: tuple[str, ...] = ()
    location: str = ""
    eligibility: Choice = ""
    sponsorship: Choice = ""
    relocation: Choice = ""
    work_model: Choice = ""
    seniority: Choice = ""
    provider: Choice = ""
    posted_within_days: int = 0
    min_score: int = 0
    min_title_match: int = 0
    min_stack_match: int = 0
    salary_floor: float = 0
    has_salary: bool = False
    exclude_ineligible: bool = False
    hide_unmet_experience: bool = False
    hide_mismatched_titles: bool = False
    hide_triaged: bool = False


@dataclass(frozen=True, slots=True)
class SnapshotReconciliationResult:
    reconciled: bool
    deactivated: int
    observed_count: int
    baseline_count: int
    confirmation_count: int
    warning: str = ""


# Artifact preparation stages, in the only order they may advance.
ARTIFACT_STAGES = ("saved", "preparing", "ready")
# Kanban columns on the pipeline board, in display order. The board is driven directly by
# jobs.status now, not by applications-table membership; "new" is the collector default and
# is never a column of its own.
# Left to right in the direction work actually flows, so the board reads as a pipeline:
# what is queued, what is out, what came back. Closed outcomes sit at the end.
PIPELINE_COLUMNS = ("bookmarked", "applied", "offer", "rejected", "not_interested")

# Sort keys are a fixed allow-list because they are interpolated into ORDER BY.
JOB_SORTS: dict[str, str] = {
    "decision_ready": (
        "CASE COALESCE(e.status, 'unknown') "
        "WHEN 'eligible' THEN 0 WHEN 'unknown' THEN 1 WHEN 'ineligible' THEN 2 ELSE 1 END ASC, "
        "COALESCE(opportunity_score, ms.total, 0) DESC"
    ),
    "opportunity": "COALESCE(opportunity_score, ms.total, 0) DESC",
    "job_fit": "COALESCE(ms.total, 0) DESC",
    "title_match": "COALESCE(json_extract(ms.dimensions_json, '$.role_domain'), 0) DESC",
    "stack_match": "COALESCE(json_extract(ms.dimensions_json, '$.stack'), 0) DESC",
    "company_fit": "COALESCE(cs.total, 0) DESC",
    "newest": "COALESCE(j.published_at, j.first_seen_at) DESC",
}

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_url TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    company_key TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    apply_url TEXT NOT NULL DEFAULT '',
    remote_scope TEXT NOT NULL DEFAULT '',
    employment_type TEXT NOT NULL DEFAULT '',
    salary_min REAL,
    salary_max REAL,
    salary_currency TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    updated_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'new',
    score_status TEXT NOT NULL DEFAULT 'pending',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    primary_source_id TEXT NOT NULL DEFAULT '',
    primary_source_priority INTEGER NOT NULL DEFAULT 0,
    merged_into_job_id INTEGER REFERENCES jobs(id),
    normalized_title TEXT NOT NULL DEFAULT '',
    location_bucket TEXT NOT NULL DEFAULT '',
    requirements_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_published ON jobs(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_score_status ON jobs(score_status);

CREATE TABLE IF NOT EXISTS job_sources (
    source_id TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source_priority INTEGER NOT NULL DEFAULT 50,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source_id, source_job_id)
);

CREATE TABLE IF NOT EXISTS eligibility (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    route TEXT NOT NULL,
    sponsorship TEXT NOT NULL,
    relocation TEXT NOT NULL,
    location_fit TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    risks_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    total INTEGER NOT NULL,
    dimensions_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    verdict TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    gaps_json TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_match_scores_job_created ON match_scores(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'saved',
    resume_path TEXT NOT NULL DEFAULT '',
    cover_letter_path TEXT NOT NULL DEFAULT '',
    packet_path TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_state (
    source_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'idle',
    last_started_at TEXT,
    last_successful_sync_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    cursor TEXT NOT NULL DEFAULT '',
    jobs_seen INTEGER NOT NULL DEFAULT 0,
    jobs_changed INTEGER NOT NULL DEFAULT 0,
    next_eligible_sync_at TEXT,
    last_skipped_reason TEXT NOT NULL DEFAULT '',
    last_truncated INTEGER NOT NULL DEFAULT 0,
    last_complete_snapshot_count INTEGER,
    pending_snapshot_count INTEGER,
    pending_snapshot_confirmations INTEGER NOT NULL DEFAULT 0,
    pending_snapshot_fingerprint TEXT NOT NULL DEFAULT '',
    last_snapshot_warning TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    jobs_seen INTEGER NOT NULL DEFAULT 0,
    jobs_new INTEGER NOT NULL DEFAULT 0,
    jobs_changed INTEGER NOT NULL DEFAULT 0,
    jobs_filtered INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    requests_made INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    skip_reason TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS duplicate_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    candidate_job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    similarity REAL NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(job_id, candidate_job_id)
);

CREATE TABLE IF NOT EXISTS api_usage (
    provider TEXT NOT NULL,
    period TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, period)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_name TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    headquarters TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    remote_policy TEXT NOT NULL DEFAULT 'unknown',
    sponsorship TEXT NOT NULL DEFAULT 'unknown',
    relocation TEXT NOT NULL DEFAULT 'unknown',
    engineering_signals_json TEXT NOT NULL DEFAULT '[]',
    risks_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    researched_at TEXT
);

CREATE TABLE IF NOT EXISTS company_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    UNIQUE(company_id, source_url)
);

CREATE TABLE IF NOT EXISTS company_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    total INTEGER NOT NULL,
    dimensions_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    risks_json TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_scores_company_created ON company_scores(company_id, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title,
    company,
    location,
    description,
    content='jobs',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts(rowid, title, company, location, description)
    VALUES (new.id, new.title, new.company, new.location, new.description);
END;

CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, company, location, description)
    VALUES ('delete', old.id, old.title, old.company, old.location, old.description);
    INSERT INTO jobs_fts(rowid, title, company, location, description)
    VALUES (new.id, new.title, new.company, new.location, new.description);
END;

CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, company, location, description)
    VALUES ('delete', old.id, old.title, old.company, old.location, old.description);
END;
"""


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    blocked = {"gh_src", "lever-source", "source", "ref", "referrer", "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term"}
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query) if key.lower() not in blocked])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def job_fingerprint(job: CollectedJob) -> str:
    canonical = canonicalize_url(job.url or job.apply_url)
    if canonical:
        material = canonical
    else:
        clean = lambda value: re.sub(r"\W+", " ", value.casefold()).strip()
        material = "|".join((clean(job.company), clean(job.title), clean(job.location)))
    return hashlib.sha256(material.encode()).hexdigest()


def content_hash(job: CollectedJob) -> str:
    raw_signals = job.metadata.get("signals")
    signals = raw_signals if isinstance(raw_signals, dict) else {}
    material = json.dumps(
        {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": job.description,
            "remote_scope": job.remote_scope,
            "employment_type": job.employment_type,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_currency": job.salary_currency,
            "eligibility_signals": signals,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def company_key(name: str) -> str:
    value = unicodedata.normalize("NFKC", name).casefold()
    tokens = re.findall(r"[^\W_]+", value, re.UNICODE)
    legal_suffixes = {"incorporated", "corporation", "company", "limited", "ltd", "llc", "inc", "gmbh", "ag", "se"}
    return "".join(token for token in tokens if token not in legal_suffixes)


def normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", value.casefold()).strip()


def location_bucket(value: str) -> str:
    text = value.casefold()
    if "worldwide" in text or "anywhere" in text:
        return "remote:worldwide"
    if "remote" in text:
        for region in ("emea", "europe", "germany", "turkiye", "turkey", "usa", "united states", "uk"):
            if region in text:
                return f"remote:{region}"
        return "remote:unknown"
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _fts_literal_query(value: str) -> str:
    terms = re.findall(r"[^\W_]+(?:[+#.][^\W_]*)?", unicodedata.normalize("NFKC", value), re.UNICODE)
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _regexp(pattern: str, value: str | None) -> bool:
    """SQLite's REGEXP operator: `X REGEXP Y` calls this as `regexp(Y, X)`."""
    return value is not None and re.search(pattern, value) is not None


def _seniority(value: str) -> str:
    text = value.casefold()
    for level in ("intern", "junior", "staff", "principal", "senior", "lead", "manager"):
        if re.search(rf"\b{level}\b", text):
            return level
    return "unspecified"


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # SQLite parses X REGEXP Y but ships no implementation, so supply one. It lets the job
        # filters reuse the scorer's own patterns instead of restating them as LIKE substrings.
        connection.create_function("regexp", 2, _regexp, deterministic=True)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_column(connection, "jobs", "company_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "jobs", "primary_source_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "jobs", "primary_source_priority", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "jobs", "merged_into_job_id", "INTEGER")
            self._ensure_column(connection, "jobs", "normalized_title", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "jobs", "location_bucket", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "jobs", "requirements_json", "TEXT NOT NULL DEFAULT '[]'")
            for name, definition in (
                ("source_priority", "INTEGER NOT NULL DEFAULT 50"),
                ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("content_hash", "TEXT NOT NULL DEFAULT ''"),
                ("active", "INTEGER NOT NULL DEFAULT 1"),
            ):
                self._ensure_column(connection, "job_sources", name, definition)
            self._ensure_column(connection, "source_state", "next_eligible_sync_at", "TEXT")
            self._ensure_column(connection, "source_state", "last_skipped_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "source_state", "last_truncated", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "source_state", "last_complete_snapshot_count", "INTEGER")
            self._ensure_column(connection, "source_state", "pending_snapshot_count", "INTEGER")
            self._ensure_column(
                connection, "source_state", "pending_snapshot_confirmations", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                connection, "source_state", "pending_snapshot_fingerprint", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(connection, "source_state", "last_snapshot_warning", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "source_sync_runs", "truncated", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "company_evidence", "etag", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "company_evidence", "last_modified", "TEXT NOT NULL DEFAULT ''")
            for row in connection.execute(
                "SELECT id, company, title, location, company_key, normalized_title, location_bucket FROM jobs"
            ).fetchall():
                normalized = (company_key(row["company"]), normalized_title(row["title"]), location_bucket(row["location"]))
                if normalized != (row["company_key"], row["normalized_title"], row["location_bucket"]):
                    connection.execute(
                        "UPDATE jobs SET company_key = ?, normalized_title = ?, location_bucket = ? WHERE id = ?",
                        (*normalized, row["id"]),
                    )
            connection.execute(
                """
                UPDATE jobs SET primary_source_id = COALESCE(
                    (SELECT source_id FROM job_sources WHERE job_id = jobs.id ORDER BY source_priority DESC LIMIT 1), ''
                ), primary_source_priority = COALESCE(
                    (SELECT source_priority FROM job_sources WHERE job_id = jobs.id ORDER BY source_priority DESC LIMIT 1), 0
                ) WHERE primary_source_id = ''
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_company_key ON jobs(company_key)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_identity ON jobs(company_key, normalized_title, location_bucket)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_merged ON jobs(merged_into_job_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_sync_runs_source ON source_sync_runs(source_id, started_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_duplicates_status ON duplicate_candidates(status, created_at DESC)")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)", (_iso(),)
            )
            if not connection.execute("SELECT 1 FROM schema_migrations WHERE version = 3").fetchone():
                # Collapse the old 7-state job status onto the 5 real pipeline columns. 'rejected'
                # changes meaning here (pre-application "not interested" -> post-application
                # "employer rejected"), so old 'rejected' rows must move first.
                for table in ("jobs", "feedback"):
                    connection.execute(f"UPDATE {table} SET status = 'not_interested' WHERE status = 'rejected'")
                    connection.execute(f"UPDATE {table} SET status = 'bookmarked' WHERE status IN ('interested', 'maybe')")
                    connection.execute(f"UPDATE {table} SET status = 'applied' WHERE status = 'interview'")
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (3, ?)", (_iso(),)
                )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def upsert_job(self, job: CollectedJob, source_priority: int = 50) -> tuple[int, bool]:
        incoming_company_key = company_key(job.company)
        if not incoming_company_key:
            raise ValueError("A job must have a non-empty Unicode company identity")
        now = _iso()
        canonical = canonicalize_url(job.url or job.apply_url)
        fingerprint = job_fingerprint(job)
        digest = content_hash(job)
        with self.connect() as connection:
            source_row = connection.execute(
                "SELECT job_id FROM job_sources WHERE source_id = ? AND source_job_id = ?",
                (job.source, job.source_job_id),
            ).fetchone()
            existing = None
            if source_row:
                existing = connection.execute("SELECT * FROM jobs WHERE id = ?", (source_row["job_id"],)).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE fingerprint = ? OR (? <> '' AND canonical_url = ?) LIMIT 1",
                    (fingerprint, canonical, canonical),
                ).fetchone()
            if existing is None:
                existing = self._strong_identity_match(connection, job)

            changed = existing is None or existing["content_hash"] != digest
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO jobs (
                        canonical_url, fingerprint, title, company, company_key, location, description, apply_url,
                        remote_scope, employment_type, salary_min, salary_max, salary_currency,
                        published_at, updated_at, first_seen_at, last_seen_at, content_hash,
                        metadata_json, primary_source_id, primary_source_priority, normalized_title, location_bucket
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical, fingerprint, job.title, job.company, incoming_company_key, job.location, job.description,
                        job.apply_url or canonical, job.remote_scope, job.employment_type,
                        job.salary_min, job.salary_max, job.salary_currency,
                        _iso(job.published_at) if job.published_at else None,
                        _iso(job.updated_at) if job.updated_at else None,
                        now, now, digest, json.dumps(job.metadata, ensure_ascii=False),
                        job.source, source_priority, normalized_title(job.title), location_bucket(job.location),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return the inserted job ID")
                job_id = int(cursor.lastrowid)
                self._record_probable_duplicates(connection, job_id, job)
            else:
                job_id = int(existing["id"])
                may_replace = job.source == existing["primary_source_id"] or source_priority > existing["primary_source_priority"]
                if changed and may_replace:
                    connection.execute(
                        """
                        UPDATE jobs SET canonical_url = ?, title = ?, company = ?, company_key = ?, location = ?,
                            description = ?, apply_url = ?, remote_scope = ?, employment_type = ?,
                            salary_min = ?, salary_max = ?, salary_currency = ?, published_at = COALESCE(?, published_at),
                            updated_at = ?, last_seen_at = ?, content_hash = ?, metadata_json = ?,
                            score_status = 'pending', active = 1, primary_source_id = ?, primary_source_priority = ?,
                            normalized_title = ?, location_bucket = ?
                        WHERE id = ?
                        """,
                        (
                            canonical or existing["canonical_url"], job.title, job.company, company_key(job.company), job.location,
                            job.description, job.apply_url or canonical, job.remote_scope, job.employment_type,
                            job.salary_min, job.salary_max, job.salary_currency,
                            _iso(job.published_at) if job.published_at else None,
                            _iso(job.updated_at) if job.updated_at else now,
                            now, digest, json.dumps(job.metadata, ensure_ascii=False), job.source, source_priority,
                            normalized_title(job.title), location_bucket(job.location), job_id,
                        ),
                    )
                else:
                    # Links are mutable provider metadata. Refresh them without re-scoring when
                    # the content and eligibility signals are otherwise unchanged.
                    if may_replace:
                        connection.execute(
                            "UPDATE jobs SET canonical_url = COALESCE(NULLIF(?, ''), canonical_url), "
                            "apply_url = COALESCE(NULLIF(?, ''), apply_url), last_seen_at = ?, active = 1 WHERE id = ?",
                            (canonical, job.apply_url or canonical, now, job_id),
                        )
                    else:
                        connection.execute("UPDATE jobs SET last_seen_at = ?, active = 1 WHERE id = ?", (now, job_id))
                    changed = False

            connection.execute(
                """
                INSERT INTO job_sources (
                    source_id, source_job_id, job_id, source_url, first_seen_at, last_seen_at,
                    source_priority, metadata_json, content_hash, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(source_id, source_job_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    source_url = excluded.source_url,
                    last_seen_at = excluded.last_seen_at,
                    source_priority = excluded.source_priority,
                    metadata_json = excluded.metadata_json,
                    content_hash = excluded.content_hash,
                    active = 1
                """,
                (job.source, job.source_job_id, job_id, job.url, now, now, source_priority, json.dumps(job.metadata), digest),
            )
            return job_id, changed

    def _strong_identity_match(self, connection: sqlite3.Connection, job: CollectedJob) -> sqlite3.Row | None:
        candidates = connection.execute(
            """
            SELECT * FROM jobs WHERE company_key = ? AND normalized_title = ? AND location_bucket = ?
              AND merged_into_job_id IS NULL ORDER BY last_seen_at DESC LIMIT 5
            """,
            (company_key(job.company), normalized_title(job.title), location_bucket(job.location)),
        ).fetchall()
        new_req = str(job.metadata.get("job_req_id") or job.metadata.get("requisition_id") or "")
        for candidate in candidates:
            old = json.loads(candidate["metadata_json"] or "{}")
            old_req = str(old.get("job_req_id") or old.get("requisition_id") or "")
            if new_req and old_req == new_req and self._dates_compatible(candidate["published_at"], job.published_at):
                return candidate
        return None

    def reconcile_source_snapshot(self, source_id: str, seen_source_job_ids: set[str]) -> int:
        """Atomically deactivate associations absent from a proven complete provider snapshot."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._reconcile_source_snapshot(connection, source_id, seen_source_job_ids)

    def reconcile_source_snapshot_guarded(
        self,
        source_id: str,
        seen_source_job_ids: set[str],
        *,
        provider_total: int | None = None,
        returned_count: int | None = None,
        drop_ratio: float = 0.5,
        minimum_baseline: int = 20,
    ) -> SnapshotReconciliationResult:
        """Reconcile a complete snapshot unless a large count drop still needs confirmation.

        The accepted baseline, pending observation, warning, and job deactivations share one
        transaction. A crash therefore cannot accept the count without applying its closures, or
        apply closures without recording the accepted count.
        """
        observed_count = len(seen_source_job_ids)
        received_count = observed_count if returned_count is None else returned_count
        if (
            isinstance(received_count, bool)
            or not isinstance(received_count, int)
            or received_count < observed_count
        ):
            raise ValueError(
                "returned_count must be an integer at least as large as the unique source-job ID count"
            )
        snapshot_fingerprint = hashlib.sha256(
            "\n".join(sorted(seen_source_job_ids)).encode("utf-8")
        ).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO source_state(source_id) VALUES (?) ON CONFLICT(source_id) DO NOTHING",
                (source_id,),
            )
            state = connection.execute(
                "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
            ).fetchone()
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM job_sources WHERE source_id = ? AND active = 1",
                (source_id,),
            ).fetchone()
            active_count = int(active["count"]) if active else 0
            stored_baseline = state["last_complete_snapshot_count"] if state else None
            baseline_count = int(stored_baseline) if stored_baseline is not None else active_count

            if provider_total is not None and provider_total > received_count:
                warning = (
                    f"Provider reported {provider_total} jobs but returned {received_count}; "
                    "missing jobs were preserved because the snapshot is incomplete."
                )
                connection.execute(
                    """
                    UPDATE source_state SET pending_snapshot_count = NULL,
                        pending_snapshot_confirmations = 0, pending_snapshot_fingerprint = '',
                        last_snapshot_warning = ?
                    WHERE source_id = ?
                    """,
                    (warning, source_id),
                )
                return SnapshotReconciliationResult(
                    False, 0, observed_count, baseline_count, 0, warning
                )

            dramatic_drop = bool(
                active_count > 0
                and (
                    observed_count == 0
                    or (
                        baseline_count >= minimum_baseline
                        and observed_count < baseline_count * drop_ratio
                    )
                )
            )
            pending = state["pending_snapshot_count"] if state else None
            confirmations = int(state["pending_snapshot_confirmations"] or 0) if state else 0
            confirmed = bool(
                dramatic_drop
                and pending is not None
                and confirmations >= 1
                and state["pending_snapshot_fingerprint"] == snapshot_fingerprint
            )
            if dramatic_drop and not confirmed:
                warning = (
                    f"Snapshot count fell from {baseline_count} to {observed_count}; missing jobs "
                    "are preserved until a second consistent complete snapshot confirms the drop."
                )
                connection.execute(
                    """
                    UPDATE source_state SET pending_snapshot_count = ?,
                        pending_snapshot_confirmations = 1, pending_snapshot_fingerprint = ?,
                        last_snapshot_warning = ?
                    WHERE source_id = ?
                    """,
                    (observed_count, snapshot_fingerprint, warning, source_id),
                )
                return SnapshotReconciliationResult(
                    False, 0, observed_count, baseline_count, 1, warning
                )

            deactivated = self._reconcile_source_snapshot(connection, source_id, seen_source_job_ids)
            connection.execute(
                """
                UPDATE source_state SET last_complete_snapshot_count = ?, pending_snapshot_count = NULL,
                    pending_snapshot_confirmations = 0, pending_snapshot_fingerprint = '',
                    last_snapshot_warning = ''
                WHERE source_id = ?
                """,
                (observed_count, source_id),
            )
            return SnapshotReconciliationResult(
                True, deactivated, observed_count, baseline_count, 2 if confirmed else 0
            )

    @staticmethod
    def _reconcile_source_snapshot(
        connection: sqlite3.Connection, source_id: str, seen_source_job_ids: set[str]
    ) -> int:
        rows = connection.execute(
            "SELECT source_job_id, job_id FROM job_sources WHERE source_id = ? AND active = 1",
            (source_id,),
        ).fetchall()
        missing = [row for row in rows if str(row["source_job_id"]) not in seen_source_job_ids]
        for row in missing:
            connection.execute(
                "UPDATE job_sources SET active = 0 WHERE source_id = ? AND source_job_id = ?",
                (source_id, row["source_job_id"]),
            )
        affected = {int(row["job_id"]) for row in missing}
        for job_id in affected:
            active = connection.execute(
                "SELECT 1 FROM job_sources WHERE job_id = ? AND active = 1 LIMIT 1", (job_id,)
            ).fetchone()
            connection.execute("UPDATE jobs SET active = ? WHERE id = ?", (1 if active else 0, job_id))
        return len(missing)

    def active_source_job_count(self, source_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM job_sources WHERE source_id = ? AND active = 1",
                (source_id,),
            ).fetchone()
            return int(row["count"]) if row else 0

    @staticmethod
    def _dates_compatible(existing: str | None, incoming: datetime | None) -> bool:
        if not existing or not incoming:
            return True
        try:
            return abs((datetime.fromisoformat(existing) - incoming).total_seconds()) <= 14 * 86400
        except ValueError:
            return True

    @staticmethod
    def _requisition_conflict(existing: sqlite3.Row, job: CollectedJob) -> bool:
        old = json.loads(existing["metadata_json"] or "{}")
        old_req = str(old.get("job_req_id") or old.get("requisition_id") or "")
        new_req = str(job.metadata.get("job_req_id") or job.metadata.get("requisition_id") or "")
        return bool(old_req and new_req and old_req != new_req)

    def _record_probable_duplicates(self, connection: sqlite3.Connection, job_id: int, job: CollectedJob) -> None:
        rows = connection.execute(
            """
            SELECT * FROM jobs WHERE id <> ? AND company_key = ? AND merged_into_job_id IS NULL
            ORDER BY last_seen_at DESC LIMIT 50
            """,
            (job_id, company_key(job.company)),
        ).fetchall()
        title = normalized_title(job.title)
        bucket = location_bucket(job.location)
        for row in rows:
            similarity = SequenceMatcher(None, title, row["normalized_title"]).ratio()
            same_place = bucket == row["location_bucket"] or "remote" in bucket or "remote" in row["location_bucket"]
            same_level = _seniority(job.title) == _seniority(row["title"])
            if similarity >= 0.90 and same_place and same_level and self._dates_compatible(row["published_at"], job.published_at):
                first, second = sorted((job_id, int(row["id"])))
                connection.execute(
                    """
                    INSERT OR IGNORE INTO duplicate_candidates
                        (job_id, candidate_job_id, similarity, reasons_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (first, second, similarity, json.dumps(["same_company", "similar_title", "compatible_location"]), _iso()),
                )

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT j.*, e.status AS eligibility_status, e.route, e.sponsorship, e.relocation,
                       e.location_fit, e.reasons_json, e.risks_json,
                       ms.total AS score, ms.dimensions_json, ms.confidence, ms.verdict,
                       ms.evidence_json, ms.gaps_json, ms.provider, ms.model,
                       c.id AS company_id, c.remote_policy AS company_remote_policy,
                       c.sponsorship AS company_sponsorship, c.relocation AS company_relocation,
                       cs.total AS company_score,
                       {OPPORTUNITY_SCORE_SQL} AS opportunity_score
                FROM jobs j
                LEFT JOIN eligibility e ON e.job_id = j.id
                LEFT JOIN match_scores ms ON ms.id = (
                    SELECT id FROM match_scores WHERE job_id = j.id ORDER BY created_at DESC, id DESC LIMIT 1
                )
                {COMPANY_SCORE_JOINS}
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
            return self._decode_row(row) if row else None

    def has_source_job(self, source_id: str, source_job_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM job_sources WHERE source_id = ? AND source_job_id = ?",
                (source_id, source_job_id),
            ).fetchone() is not None

    def matching_job_id(self, job: CollectedJob) -> int | None:
        canonical = canonicalize_url(job.url or job.apply_url)
        with self.connect() as connection:
            source = connection.execute(
                "SELECT job_id FROM job_sources WHERE source_id = ? AND source_job_id = ?",
                (job.source, job.source_job_id),
            ).fetchone()
            if source:
                return int(source["job_id"])
            row = connection.execute(
                "SELECT id FROM jobs WHERE fingerprint = ? OR (? <> '' AND canonical_url = ?) LIMIT 1",
                (job_fingerprint(job), canonical, canonical),
            ).fetchone()
            if row:
                return int(row["id"])
            strong = self._strong_identity_match(connection, job)
            return int(strong["id"]) if strong else None

    @staticmethod
    def _job_filters(filters: JobFilters) -> tuple[list[str], list[Any], str]:
        """Build the shared WHERE clauses so the list and its total count can never disagree."""
        clauses = ["j.active = 1", "j.merged_into_job_id IS NULL", f"COALESCE({OPPORTUNITY_SCORE_SQL}, 0) >= ?"]
        params: list[Any] = [filters.min_score]
        for column, chosen in (
            ("e.route", _chosen(filters.route)),
            ("j.status", _chosen(filters.status)),
            ("COALESCE(e.status, 'unknown')", _chosen(filters.eligibility)),
            ("COALESCE(e.sponsorship, 'unknown')", _chosen(filters.sponsorship)),
            ("COALESCE(e.relocation, 'unknown')", _chosen(filters.relocation)),
        ):
            if chosen:
                clauses.append(f"{column} IN ({','.join('?' for _ in chosen)})")
                params.extend(chosen)
        if filters.source_ids:
            placeholders = ",".join("?" for _ in filters.source_ids)
            clauses.append(f"EXISTS (SELECT 1 FROM job_sources js WHERE js.job_id = j.id AND js.source_id IN ({placeholders}))")
            params.extend(filters.source_ids)
        if filters.exclude_ineligible:
            clauses.append("COALESCE(e.status, 'unknown') <> 'ineligible'")
        if filters.hide_unmet_experience:
            # Each stored requirement carries its own "unmet" bool (see extract_experience_requirements
            # in scoring.py) - works identically in rules and LLM mode, unlike matching gap text.
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM json_each(j.requirements_json) req WHERE json_extract(req.value, '$.unmet') = 1)"
            )
        work_models = {
            "remote_worldwide": "j.location_bucket = 'remote:worldwide'",
            "remote": "j.location_bucket LIKE 'remote:%'",
            "onsite": "j.location_bucket NOT LIKE 'remote:%'",
        }
        wanted = [work_models[value] for value in _chosen(filters.work_model) if value in work_models]
        if wanted:
            clauses.append(f"({' OR '.join(wanted)})")
        seniorities = [value for value in _chosen(filters.seniority) if seniority_title_pattern(value)]
        if seniorities:
            clauses.append(f"({' OR '.join('j.normalized_title REGEXP ?' for _ in seniorities)})")
            params.extend(seniority_title_pattern(value) for value in seniorities)
        if filters.title:
            clauses.append("j.normalized_title LIKE ?")
            params.append(f"%{filters.title.casefold()}%")
        for technology in filters.technologies:
            clauses.append("(j.title LIKE ? OR j.description LIKE ?)")
            params.extend([f"%{technology}%", f"%{technology}%"])
        if filters.company:
            clauses.append("j.company_key = ?")
            params.append(company_key(filters.company))
        if filters.location:
            clauses.append("j.location LIKE ?")
            params.append(f"%{filters.location}%")
        if filters.company_in:
            placeholders = ",".join("?" for _ in filters.company_in)
            clauses.append(f"j.company_key IN ({placeholders})")
            params.extend(filters.company_in)
        if filters.posted_within_days > 0:
            clauses.append(
                "julianday(COALESCE(j.published_at, j.first_seen_at)) >= julianday('now', ?)"
            )
            params.append(f"-{int(filters.posted_within_days)} days")
        if filters.has_salary:
            clauses.append("(j.salary_min IS NOT NULL OR j.salary_max IS NOT NULL)")
        if filters.salary_floor > 0:
            clauses.append("COALESCE(j.salary_max, j.salary_min, 0) >= ?")
            params.append(filters.salary_floor)
        if filters.min_title_match > 0:
            clauses.append("COALESCE(json_extract(ms.dimensions_json, '$.role_domain'), 0) >= ?")
            params.append(filters.min_title_match)
        if filters.min_stack_match > 0:
            clauses.append("COALESCE(json_extract(ms.dimensions_json, '$.stack'), 0) >= ?")
            params.append(filters.min_stack_match)
        providers = _chosen(filters.provider)
        if providers:
            # "Language model" is a family, not one provider: llm.py stores "ollama" for the
            # documented default mode and "openai-compatible" for every other endpoint. Matching
            # any scored non-rules provider keeps the filter correct if a third one is added.
            clauses.append("({})".format(" OR ".join(
                "COALESCE(ms.provider, '') NOT IN ('', 'rules')" if value == MODEL_SCORED_PROVIDER_FILTER
                else "COALESCE(ms.provider, '') = ?"
                for value in providers
            )))
            params.extend(value for value in providers if value != MODEL_SCORED_PROVIDER_FILTER)
        if filters.hide_triaged:
            # Everything the user has already made a decision about - bookmarked, applied, or set
            # aside. Only ever set when no explicit status was chosen (see _job_filters_from_query),
            # so ticking Bookmarked in the Status facet still shows bookmarks.
            clauses.append("j.status = 'new'")
        if filters.hide_mismatched_titles:
            # role_domain <= 9 is "unrelated" in both rule-based scoring (_role_match returns 2 or
            # 6 for a different role family, never 10-21) and the LLM prompt's own documented
            # bucket (0-9 unrelated). Default to the max (same family) for a not-yet-scored job so
            # pending jobs are never hidden before they get a chance to be seen.
            clauses.append("COALESCE(json_extract(ms.dimensions_json, '$.role_domain'), 30) > 9")
        join_fts = ""
        if filters.query:
            literal_query = _fts_literal_query(filters.query)
            if literal_query:
                join_fts = "JOIN jobs_fts f ON f.rowid = j.id"
                clauses.append("jobs_fts MATCH ?")
                params.append(literal_query)
        return clauses, params, join_fts

    def facet_counts(self, filters: JobFilters | None = None) -> dict[str, dict[str, int]]:
        """How many jobs each facet value would leave, given every other filter.

        Narrowing a 13,000-row corpus is guesswork without this: the menu offers eight seniority
        levels and no hint that one of them matches nothing. Each facet is counted with its own
        selections dropped — standard drill-down semantics — so a count reads as "what ticking
        this would give me", including for a facet that already has something ticked.

        Only facets that count as one indexed comparison are here. Seniority runs a REGEXP per
        row and technology a LIKE over every description; counting those costs seconds, so their
        menus stay uncounted rather than making the page wait.
        """
        base = filters or JobFilters()
        counts: dict[str, dict[str, int]] = {}
        with self.connect() as connection:
            for facet, values in FACET_VALUE_SQL.items():
                dropped: dict[str, Any] = {FACET_FILTER_FIELDS[facet]: ()}
                clauses, params, join_fts = self._job_filters(replace(base, **dropped))
                selected = ", ".join(
                    f"SUM(CASE WHEN {sql} THEN 1 ELSE 0 END) AS value_{index}"
                    for index, sql in enumerate(values.values())
                )
                row = connection.execute(
                    f"""
                    SELECT {selected}
                    FROM jobs j
                    {join_fts}
                    LEFT JOIN eligibility e ON e.job_id = j.id
                    LEFT JOIN match_scores ms ON ms.id = (
                        SELECT id FROM match_scores WHERE job_id = j.id ORDER BY created_at DESC, id DESC LIMIT 1
                    )
                    {COMPANY_SCORE_JOINS}
                    WHERE {' AND '.join(clauses)}
                    """,
                    params,
                ).fetchone()
                counts[facet] = {
                    value: int(row[f"value_{index}"] or 0) for index, value in enumerate(values)
                }
        return counts

    def count_jobs(self, filters: JobFilters | None = None) -> int:
        """Total matches ignoring limit/offset, so the UI can show a real count instead of a cap."""
        clauses, params, join_fts = self._job_filters(filters or JobFilters())
        sql = f"""
            SELECT COUNT(*) AS total
            FROM jobs j
            {join_fts}
            LEFT JOIN eligibility e ON e.job_id = j.id
            LEFT JOIN match_scores ms ON ms.id = (
                SELECT id FROM match_scores WHERE job_id = j.id ORDER BY created_at DESC, id DESC LIMIT 1
            )
            {COMPANY_SCORE_JOINS}
            WHERE {' AND '.join(clauses)}
        """
        with self.connect() as connection:
            return int(connection.execute(sql, params).fetchone()["total"])

    def list_jobs(
        self,
        filters: JobFilters | None = None,
        *,
        sort: str = "decision_ready",
        limit: int | None = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params, join_fts = self._job_filters(filters or JobFilters())
        pagination = ""
        if limit is not None:
            pagination = "LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        sql = f"""
            SELECT j.*, e.status AS eligibility_status, e.route, e.sponsorship, e.relocation,
                   e.location_fit, e.reasons_json, e.risks_json,
                   ms.total AS score, ms.dimensions_json, ms.confidence, ms.verdict,
                   ms.evidence_json, ms.gaps_json, ms.provider, ms.model,
                   c.id AS company_id, c.remote_policy AS company_remote_policy,
                   c.sponsorship AS company_sponsorship, c.relocation AS company_relocation,
                   cs.total AS company_score,
                   {OPPORTUNITY_SCORE_SQL} AS opportunity_score
            FROM jobs j
            {join_fts}
            LEFT JOIN eligibility e ON e.job_id = j.id
            LEFT JOIN match_scores ms ON ms.id = (
                SELECT id FROM match_scores WHERE job_id = j.id ORDER BY created_at DESC, id DESC LIMIT 1
            )
            {COMPANY_SCORE_JOINS}
            WHERE {' AND '.join(clauses)}
            ORDER BY {JOB_SORTS.get(sort) or JOB_SORTS["decision_ready"]},
                     COALESCE(opportunity_score, ms.total, 0) DESC,
                     COALESCE(j.published_at, j.first_seen_at) DESC
            {pagination}
        """
        with self.connect() as connection:
            return [self._decode_row(row) for row in connection.execute(sql, params).fetchall()]

    def list_job_sources(self, job_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Load every provenance association for a set of jobs without one query per job."""
        result: dict[int, list[dict[str, Any]]] = {job_id: [] for job_id in job_ids}
        with self.connect() as connection:
            for start in range(0, len(job_ids), 500):
                batch = job_ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT job_id, source_id, source_job_id, source_url, first_seen_at, last_seen_at,
                           source_priority, metadata_json, active
                    FROM job_sources
                    WHERE job_id IN ({placeholders})
                    ORDER BY job_id, active DESC, source_priority DESC, source_id, source_job_id
                    """,
                    batch,
                ).fetchall()
                for row in rows:
                    source = dict(row)
                    source["metadata"] = json.loads(source.pop("metadata_json") or "{}")
                    source["active"] = bool(source["active"])
                    result[int(source.pop("job_id"))].append(source)
        return result

    def pending_job_ids(self, prompt_version: str = "", limit: int = 1000) -> list[int]:
        version_clause = ""
        params: list[Any] = []
        if prompt_version:
            version_clause = """
                OR COALESCE((
                    SELECT prompt_version FROM match_scores
                    WHERE job_id = jobs.id ORDER BY created_at DESC, id DESC LIMIT 1
                ), '') <> ?
            """
            params.append(prompt_version)
        params.append(limit)
        with self.connect() as connection:
            return [
                int(row["id"])
                for row in connection.execute(
                    f"""
                    SELECT id FROM jobs
                    WHERE active = 1 AND merged_into_job_id IS NULL
                      AND (score_status IN ('pending', 'pending_llm') {version_clause})
                    ORDER BY first_seen_at LIMIT ?
                    """,
                    params,
                ).fetchall()
            ]

    def save_evaluation(
        self,
        job_id: int,
        eligibility: EligibilityResult,
        score: ScoreResult,
        status: str,
        requirements: list[dict[str, Any]] | None = None,
    ) -> None:
        now = _iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO eligibility (
                    job_id, status, route, sponsorship, relocation, location_fit,
                    reasons_json, risks_json, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status, route = excluded.route,
                    sponsorship = excluded.sponsorship, relocation = excluded.relocation,
                    location_fit = excluded.location_fit, reasons_json = excluded.reasons_json,
                    risks_json = excluded.risks_json, evaluated_at = excluded.evaluated_at
                """,
                (
                    job_id, eligibility.status.value, eligibility.route,
                    eligibility.sponsorship, eligibility.relocation, eligibility.location_fit,
                    json.dumps(eligibility.reasons), json.dumps(eligibility.risks), now,
                ),
            )
            connection.execute(
                """
                INSERT INTO match_scores (
                    job_id, total, dimensions_json, confidence, verdict, evidence_json,
                    gaps_json, provider, model, prompt_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, score.total, json.dumps(score.dimensions), score.confidence,
                    score.verdict, json.dumps(score.evidence), json.dumps(score.gaps),
                    score.provider, score.model, score.prompt_version, now,
                ),
            )
            connection.execute(
                "UPDATE jobs SET score_status = ?, requirements_json = ? WHERE id = ?",
                (status, json.dumps(requirements or []), job_id),
            )

    def save_feedback(self, job_id: int, status: JobStatus, reason: str = "") -> None:
        now = _iso()
        with self.connect() as connection:
            connection.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job_id))
            connection.execute(
                "INSERT INTO feedback (job_id, status, reason, created_at) VALUES (?, ?, ?, ?)",
                (job_id, status.value, reason.strip(), now),
            )

    @staticmethod
    def _artifact_stage(column: str) -> str:
        """Artifact preparation only moves forward, so regenerating a resume cannot undo a prepared packet."""
        cases = " ".join(
            f"WHEN {column} = '{stage}' THEN {index}" for index, stage in enumerate(ARTIFACT_STAGES)
        )
        return f"(CASE {cases} ELSE 0 END)"

    def save_application(
        self,
        job_id: int,
        *,
        status: str,
        resume_path: str | None = None,
        cover_letter_path: str | None = None,
        packet_path: str | None = None,
        notes: str | None = None,
    ) -> None:
        now = _iso()
        with self.connect() as connection:
            # A missing argument is NULL and keeps what is stored, so a caller that regenerates one
            # artifact never blanks the others. An empty string is a real value and clears the
            # column, which the old "empty means keep" sentinel made impossible to express - a note
            # or a path pointing at a deleted file could never be taken back.
            connection.execute(
                f"""
                INSERT INTO applications (
                    job_id, status, resume_path, cover_letter_path, packet_path, notes, created_at, updated_at
                ) VALUES (
                    :job_id, :status, COALESCE(:resume_path, ''), COALESCE(:cover_letter_path, ''),
                    COALESCE(:packet_path, ''), COALESCE(:notes, ''), :now, :now
                )
                ON CONFLICT(job_id) DO UPDATE SET
                    status = CASE
                        WHEN {self._artifact_stage('excluded.status')} > {self._artifact_stage('applications.status')}
                        THEN excluded.status ELSE applications.status END,
                    resume_path = COALESCE(:resume_path, applications.resume_path),
                    cover_letter_path = COALESCE(:cover_letter_path, applications.cover_letter_path),
                    packet_path = COALESCE(:packet_path, applications.packet_path),
                    notes = COALESCE(:notes, applications.notes),
                    updated_at = :now
                """,
                {
                    "job_id": job_id, "status": status, "resume_path": resume_path,
                    "cover_letter_path": cover_letter_path, "packet_path": packet_path,
                    "notes": notes, "now": now,
                },
            )

    def list_applications(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT a.*, j.title, j.company, j.canonical_url, j.status AS job_status, e.route,
                           (SELECT total FROM match_scores WHERE job_id = j.id ORDER BY created_at DESC, id DESC LIMIT 1) AS score
                    FROM applications a
                    JOIN jobs j ON j.id = a.job_id
                    LEFT JOIN eligibility e ON e.job_id = j.id
                    ORDER BY a.updated_at DESC
                    """
                ).fetchall()
            ]

    def start_source(self, source_id: str) -> None:
        # Clears last_error/last_skipped_reason so a fresh attempt never displays a stale note
        # from a previous run's different outcome (e.g. "minimum sync interval" left showing
        # next to a source that is now actually running or has since failed for another reason).
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_state (source_id, status, last_started_at)
                VALUES (?, 'running', ?)
                ON CONFLICT(source_id) DO UPDATE SET status = 'running', last_started_at = excluded.last_started_at,
                    last_error = '', last_skipped_reason = ''
                """,
                (source_id, _iso()),
            )

    def start_sync_run(self, source_id: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO source_sync_runs(source_id, started_at, status) VALUES (?, ?, 'running')",
                (source_id, _iso()),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return the sync run ID")
            return int(cursor.lastrowid)

    def reset_stale_sync_runs(self) -> int:
        """Close source attempts left running by a cancelled or terminated process.

        A new SyncService run owns the only in-process sync lock, so any pre-existing running
        row necessarily belongs to an interrupted older run. Leaving it as running forever makes
        the Sources page report work that no process can complete.
        """
        now = _iso()
        message = "Previous sync was interrupted before completion"
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE source_sync_runs SET finished_at = ?, status = 'error', error = ?
                WHERE status = 'running'
                """,
                (now, message),
            )
            connection.execute(
                """
                UPDATE source_state SET status = 'error', last_error = ?, next_eligible_sync_at = NULL
                WHERE status = 'running'
                """,
                (message,),
            )
            return cursor.rowcount

    def finish_sync_run(self, run_id: int, *, status: str, started_at: datetime, **metrics: Any) -> None:
        duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE source_sync_runs SET finished_at = ?, status = ?, jobs_seen = ?, jobs_new = ?,
                    jobs_changed = ?, jobs_filtered = ?, duplicates = ?, requests_made = ?,
                    duration_ms = ?, error = ?, skip_reason = ?, truncated = ? WHERE id = ?
                """,
                (
                    _iso(), status, int(metrics.get("jobs_seen", 0)), int(metrics.get("jobs_new", 0)),
                    int(metrics.get("jobs_changed", 0)), int(metrics.get("jobs_filtered", 0)),
                    int(metrics.get("duplicates", 0)), int(metrics.get("requests_made", 0)), duration_ms,
                    str(metrics.get("error", ""))[:1000], str(metrics.get("skip_reason", ""))[:500],
                    int(bool(metrics.get("truncated", False))), run_id,
                ),
            )

    def skip_source(self, source_id: str, reason: str, next_eligible: datetime | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_state(source_id, status, last_skipped_reason, next_eligible_sync_at)
                VALUES (?, 'skipped', ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET status = 'skipped',
                    last_skipped_reason = excluded.last_skipped_reason,
                    next_eligible_sync_at = excluded.next_eligible_sync_at
                """,
                (source_id, reason[:500], _iso(next_eligible) if next_eligible else None),
            )

    def reserve_api_requests(self, provider: str, count: int, monthly_limit: int) -> bool:
        if monthly_limit <= 0:
            return False
        period = datetime.now(UTC).strftime("%Y-%m")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_count FROM api_usage WHERE provider = ? AND period = ?", (provider, period)
            ).fetchone()
            used = int(row["request_count"]) if row else 0
            if used + count > monthly_limit:
                return False
            connection.execute(
                """
                INSERT INTO api_usage(provider, period, request_count, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, period) DO UPDATE SET
                    request_count = api_usage.request_count + excluded.request_count,
                    updated_at = excluded.updated_at
                """,
                (provider, period, count, _iso()),
            )
            return True

    def list_api_usage(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM api_usage ORDER BY period DESC, provider"
            ).fetchall()]

    def finish_source(
        self,
        source_id: str,
        seen: int,
        changed: int,
        cursor: str = "",
        truncated: bool = False,
        snapshot_warning: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_state (
                    source_id, status, last_started_at, last_successful_sync_at, cursor, jobs_seen, jobs_changed,
                    last_truncated, last_snapshot_warning
                ) VALUES (?, 'idle', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    status = 'idle', last_successful_sync_at = excluded.last_successful_sync_at,
                    last_error = '', cursor = excluded.cursor, jobs_seen = excluded.jobs_seen,
                    jobs_changed = excluded.jobs_changed, next_eligible_sync_at = NULL,
                    last_skipped_reason = '', last_truncated = excluded.last_truncated,
                    last_snapshot_warning = excluded.last_snapshot_warning
                """,
                (source_id, _iso(), _iso(), cursor, seen, changed, int(truncated), snapshot_warning[:1000]),
            )

    def fail_source(self, source_id: str, error: str, retry_seconds: int = 900) -> None:
        retry_at = datetime.now(UTC) + timedelta(seconds=retry_seconds)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_state (source_id, status, last_started_at, last_error, next_eligible_sync_at)
                VALUES (?, 'error', ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET status = 'error', last_error = excluded.last_error,
                    next_eligible_sync_at = excluded.next_eligible_sync_at
                """,
                (source_id, _iso(), error[:1000], _iso(retry_at)),
            )

    def source_state(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM source_state WHERE source_id = ?", (source_id,)).fetchone()
            return dict(row) if row else None

    def list_sources(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                """
                SELECT s.*, r.jobs_new AS last_jobs_new, r.jobs_filtered AS last_jobs_filtered,
                       r.duplicates AS last_duplicates, r.requests_made AS last_requests_made,
                       r.duration_ms AS last_duration_ms
                FROM source_state s LEFT JOIN source_sync_runs r ON r.id = (
                    SELECT id FROM source_sync_runs WHERE source_id = s.source_id AND status <> 'skipped'
                    ORDER BY id DESC LIMIT 1
                ) ORDER BY s.source_id
                """
            ).fetchall()]

    def list_duplicate_candidates(self, status: str = "open") -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                """
                SELECT d.*, a.title AS job_title, a.company AS job_company, a.location AS job_location,
                       b.title AS candidate_title, b.company AS candidate_company, b.location AS candidate_location
                FROM duplicate_candidates d
                JOIN jobs a ON a.id = d.job_id JOIN jobs b ON b.id = d.candidate_job_id
                WHERE d.status = ? ORDER BY d.similarity DESC, d.created_at DESC
                """,
                (status,),
            ).fetchall()]

    def list_duplicate_clusters(self, status: str = "open", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Open candidates grouped into one decision per set of jobs that are the same posting.

        Four copies of one Amazon posting produce six pair rows, and a reviewer answering them
        one at a time answers the same question six times. The question is which copy to keep,
        asked once per set, so the pairs are joined back into the set they came from.
        """
        candidates = self.list_duplicate_candidates(status)
        parent: dict[int, int] = {}

        def root(job_id: int) -> int:
            parent.setdefault(job_id, job_id)
            while parent[job_id] != job_id:
                parent[job_id] = parent[parent[job_id]]
                job_id = parent[job_id]
            return job_id

        for item in candidates:
            first, second = root(int(item["job_id"])), root(int(item["candidate_job_id"]))
            if first != second:
                parent[first] = second
        grouped: dict[int, list[dict[str, Any]]] = {}
        for item in candidates:
            grouped.setdefault(root(int(item["job_id"])), []).append(item)
        clusters: list[dict[str, Any]] = [
            {
                "candidate_ids": sorted(int(item["id"]) for item in members),
                "job_ids": sorted({int(item[key]) for item in members for key in ("job_id", "candidate_job_id")}),
                "similarity": min(float(item["similarity"]) for item in members),
            }
            for members in grouped.values()
        ]
        clusters.sort(key=lambda cluster: (-cluster["similarity"], cluster["candidate_ids"][0]))
        page = clusters[offset : offset + limit]
        jobs = self._duplicate_cluster_jobs([job_id for cluster in page for job_id in cluster["job_ids"]])
        for cluster in page:
            members = [jobs[job_id] for job_id in cluster["job_ids"] if job_id in jobs]
            cluster["jobs"] = members
            # Repeating the fields every copy agrees on is what made the old table unreadable:
            # they belong to the set, and only the fields that differ belong to a row.
            cluster["shared"] = {
                field: members[0][field] if members and len({item[field] for item in members}) == 1 else ""
                for field in ("title", "company", "location")
            }
        return {
            "clusters": [cluster for cluster in page if cluster["jobs"]],
            "total": len(clusters),
            "exact_total": sum(1 for cluster in clusters if cluster["similarity"] >= 0.999),
        }

    def _duplicate_cluster_jobs(self, job_ids: list[int]) -> dict[int, dict[str, Any]]:
        """What actually distinguishes one copy from another: where it came from, when it arrived,
        and whether the reader has already put work into it."""
        if not job_ids:
            return {}
        placeholders = ",".join("?" for _ in job_ids)
        with self.connect() as connection:
            return {
                int(row["id"]): dict(row)
                for row in connection.execute(
                    f"""
                    SELECT j.id, j.title, j.company, j.location, j.canonical_url, j.primary_source_id,
                           j.first_seen_at, j.status, j.active,
                           (SELECT COUNT(*) FROM applications a
                             WHERE a.job_id = j.id AND (a.resume_path != '' OR a.packet_path != '' OR a.cover_letter_path != '')
                           ) AS artifact_count
                    FROM jobs j WHERE j.id IN ({placeholders})
                    """,
                    job_ids,
                ).fetchall()
            }

    def dismiss_duplicate(self, candidate_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE duplicate_candidates SET status = 'dismissed', decided_at = ? WHERE id = ?",
                (_iso(), candidate_id),
            )

    def merge_duplicate(self, candidate_id: int, keep_job_id: int | None = None) -> int:
        with self.connect() as connection:
            candidate = connection.execute(
                "SELECT * FROM duplicate_candidates WHERE id = ? AND status = 'open'", (candidate_id,)
            ).fetchone()
            if not candidate:
                raise LookupError("Duplicate candidate not found")
            ids = (int(candidate["job_id"]), int(candidate["candidate_job_id"]))
            winner = keep_job_id if keep_job_id in ids else ids[0]
            loser = ids[1] if winner == ids[0] else ids[0]
            active_count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE id IN (?, ?) AND active = 1", (winner, loser)
            ).fetchone()[0]
            if active_count != 2:
                # One side was already merged away by a different pair that shared it - this
                # candidacy is stale, not actionable (merging into, or out of, a job that no
                # longer has its own active record would just bury the real winner a hop
                # deeper). Dismiss it here so it stops lingering in the review queue.
                connection.execute(
                    "UPDATE duplicate_candidates SET status = 'dismissed', decided_at = ? WHERE id = ?",
                    (_iso(), candidate_id),
                )
                # connect() only commits on a clean exit of the `with` block, so the dismissal
                # above would be rolled back along with the exception below unless committed here.
                connection.commit()
                raise ValueError("One or both jobs were already merged elsewhere; this candidate is now stale")
            loser_application = connection.execute("SELECT * FROM applications WHERE job_id = ?", (loser,)).fetchone()
            winner_application = connection.execute("SELECT * FROM applications WHERE job_id = ?", (winner,)).fetchone()
            artifact_columns = ("resume_path", "cover_letter_path", "packet_path", "notes")
            if loser_application and winner_application and any(loser_application[key] for key in artifact_columns) and any(
                winner_application[key] for key in artifact_columns
            ):
                raise ValueError("Both jobs contain application work; resolve their artifacts before merging")
            connection.execute("UPDATE job_sources SET job_id = ? WHERE job_id = ?", (winner, loser))
            connection.execute("UPDATE feedback SET job_id = ? WHERE job_id = ?", (winner, loser))
            if loser_application and not winner_application:
                connection.execute("UPDATE applications SET job_id = ? WHERE job_id = ?", (winner, loser))
            elif loser_application:
                connection.execute(
                    """
                    UPDATE applications SET
                      resume_path = COALESCE(NULLIF(resume_path, ''), ?),
                      cover_letter_path = COALESCE(NULLIF(cover_letter_path, ''), ?),
                      packet_path = COALESCE(NULLIF(packet_path, ''), ?),
                      notes = COALESCE(NULLIF(notes, ''), ?), updated_at = ?
                    WHERE job_id = ?
                    """,
                    (*(loser_application[key] for key in artifact_columns), _iso(), winner),
                )
                connection.execute("DELETE FROM applications WHERE job_id = ?", (loser,))
            connection.execute("UPDATE jobs SET active = 0, merged_into_job_id = ? WHERE id = ?", (winner, loser))
            connection.execute(
                "UPDATE duplicate_candidates SET status = 'merged', decided_at = ? WHERE id = ?", (_iso(), candidate_id)
            )
            # Any other open candidate that names the job we just merged away is stale the
            # instant this commits, at any similarity - dismiss it so it does not keep
            # showing up as actionable once one side of the pair no longer exists.
            connection.execute(
                "UPDATE duplicate_candidates SET status = 'dismissed', decided_at = ? "
                "WHERE status = 'open' AND id != ? AND (job_id = ? OR candidate_job_id = ?)",
                (_iso(), candidate_id, loser, loser),
            )
            return winner

    def merge_all_exact_duplicates(self) -> dict[str, int]:
        """Merge every open duplicate candidate that is an exact (100%) match, keeping each pair's
        first job. merge_duplicate() itself now refuses (and dismisses) a candidate once either of
        its jobs was already merged away earlier in this same batch, so this loop just needs to
        keep going and count what happened."""
        with self.connect() as connection:
            candidate_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM duplicate_candidates WHERE status = 'open' AND similarity >= 0.999 ORDER BY id"
                ).fetchall()
            ]
        merged = skipped = 0
        for candidate_id in candidate_ids:
            try:
                self.merge_duplicate(candidate_id)
                merged += 1
            except (LookupError, ValueError):
                skipped += 1
        return {"merged": merged, "skipped": skipped}

    def company_jobs(self, name: str, limit: int = 100) -> list[dict[str, Any]]:
        key = company_key(name)
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM jobs WHERE company_key = ? AND active = 1 AND merged_into_job_id IS NULL ORDER BY published_at DESC LIMIT ?",
                    (key, limit),
                ).fetchall()
            ]

    def company_evidence_cache(self, name: str) -> dict[str, dict[str, Any]]:
        """What was stored for each official page last time, so a refresh can revalidate it."""
        with self.connect() as connection:
            return {
                row["source_url"]: dict(row)
                for row in connection.execute(
                    """
                    SELECT e.source_url, e.source_type, e.title, e.excerpt, e.etag, e.last_modified
                    FROM company_evidence e
                    JOIN companies c ON c.id = e.company_id
                    WHERE c.normalized_name = ?
                    """,
                    (company_key(name),),
                ).fetchall()
            }

    def save_company_research(
        self,
        *,
        name: str,
        domain: str,
        profile: dict[str, Any],
        evidence: list[dict[str, str]],
        score: dict[str, Any],
        provider: str,
        model: str,
    ) -> int:
        now = _iso()
        key = company_key(name)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO companies (
                    normalized_name, name, domain, summary, industry, headquarters, size,
                    remote_policy, sponsorship, relocation, engineering_signals_json,
                    risks_json, confidence, status, researched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)
                ON CONFLICT(normalized_name) DO UPDATE SET
                    name = excluded.name, domain = excluded.domain, summary = excluded.summary,
                    industry = excluded.industry, headquarters = excluded.headquarters, size = excluded.size,
                    remote_policy = excluded.remote_policy, sponsorship = excluded.sponsorship,
                    relocation = excluded.relocation, engineering_signals_json = excluded.engineering_signals_json,
                    risks_json = excluded.risks_json, confidence = excluded.confidence,
                    status = 'ready', researched_at = excluded.researched_at
                """,
                (
                    key, name, domain, profile.get("summary", ""), profile.get("industry", ""),
                    profile.get("headquarters", ""), profile.get("size", ""),
                    profile.get("remote_policy", "unknown"), profile.get("sponsorship", "unknown"),
                    profile.get("relocation", "unknown"), json.dumps(profile.get("engineering_signals", [])),
                    json.dumps(profile.get("risks", [])), float(profile.get("confidence", 0)), now,
                ),
            )
            company_id = int(connection.execute("SELECT id FROM companies WHERE normalized_name = ?", (key,)).fetchone()["id"])
            connection.execute("DELETE FROM company_evidence WHERE company_id = ?", (company_id,))
            for item in evidence:
                excerpt = item.get("excerpt", "")
                connection.execute(
                    """
                    INSERT INTO company_evidence (
                        company_id, source_url, source_type, title, excerpt, content_hash, fetched_at,
                        etag, last_modified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(company_id, source_url) DO UPDATE SET
                        source_type = excluded.source_type, title = excluded.title,
                        excerpt = excluded.excerpt, content_hash = excluded.content_hash,
                        fetched_at = excluded.fetched_at, etag = excluded.etag,
                        last_modified = excluded.last_modified
                    """,
                    (
                        company_id, item["source_url"], item.get("source_type", "official"),
                        item.get("title", ""), excerpt,
                        hashlib.sha256(excerpt.encode()).hexdigest(), now,
                        item.get("etag", ""), item.get("last_modified", ""),
                    ),
                )
            connection.execute(
                """
                INSERT INTO company_scores (
                    company_id, total, dimensions_json, reasons_json, risks_json,
                    provider, model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id, int(score["total"]), json.dumps(score["dimensions"]),
                    json.dumps(score.get("reasons", [])), json.dumps(score.get("risks", [])),
                    provider, model, now,
                ),
            )
            return company_id

    def get_company(self, company_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT c.*, cs.total AS score, cs.dimensions_json, cs.reasons_json AS score_reasons_json,
                       cs.risks_json AS score_risks_json, cs.provider, cs.model
                FROM companies c
                LEFT JOIN company_scores cs ON cs.id = (
                    SELECT id FROM company_scores WHERE company_id = c.id ORDER BY created_at DESC, id DESC LIMIT 1
                )
                WHERE c.id = ?
                """,
                (company_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            for key in ("engineering_signals_json", "risks_json", "dimensions_json", "score_reasons_json", "score_risks_json"):
                if result.get(key):
                    result[key.removesuffix("_json")] = json.loads(result[key])
            result["evidence"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM company_evidence WHERE company_id = ? ORDER BY fetched_at DESC",
                    (company_id,),
                ).fetchall()
            ]
            result["jobs"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT id, title, location, status FROM jobs WHERE company_key = ? AND active = 1 ORDER BY published_at DESC LIMIT 50",
                    (result["normalized_name"],),
                ).fetchall()
            ]
            official_types = {
                item["source_type"]
                for item in result["evidence"]
                if item["source_type"] not in {"current_job_posting", "public_registry"}
            }
            official_count = sum(
                item["source_type"] not in {"current_job_posting", "public_registry"}
                for item in result["evidence"]
            )
            registry_count = sum(item["source_type"] == "public_registry" for item in result["evidence"])
            result["coverage_label"] = (
                "strong" if len(official_types) >= 2 else "moderate" if official_types
                else "limited" if registry_count or len(result["evidence"]) >= 2 else "low"
            )
            result["official_evidence_type_count"] = len(official_types)
            result["official_evidence_count"] = official_count
            result["evidence_count"] = len(result["evidence"])
            salary_known = connection.execute(
                "SELECT 1 FROM jobs WHERE company_key = ? AND active = 1 AND (salary_min IS NOT NULL OR salary_max IS NOT NULL) LIMIT 1",
                (result["normalized_name"],),
            ).fetchone() is not None
            result["job_texts_sampled"] = sum(
                item["source_type"] == "current_job_posting" for item in result["evidence"]
            )
            result["fact_coverage_factors"] = [
                {"name": "Remote policy", "state": "unknown" if result["remote_policy"] == "unknown" else "established", "points": 0 if result["remote_policy"] == "unknown" else 2},
                {"name": "Sponsorship", "state": "unknown" if result["sponsorship"] == "unknown" else "contradicted" if result["sponsorship"] == "unavailable" else "established", "points": 0 if result["sponsorship"] == "unknown" else 2},
                {"name": "Relocation", "state": "unknown" if result["relocation"] == "unknown" else "contradicted" if result["relocation"] == "unavailable" else "established", "points": 0 if result["relocation"] == "unknown" else 2},
                {"name": "Compensation", "state": "established" if salary_known else "unknown", "points": 2 if salary_known else 0},
                {"name": "Engineering signals", "state": "established" if result.get("engineering_signals") else "unknown", "points": 2 if result.get("engineering_signals") else 0},
            ]
            return result

    def list_companies(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT c.*, s.total AS score, s.provider, s.model,
                           (SELECT COUNT(*) FROM company_evidence WHERE company_id = c.id) AS evidence_count,
                           (SELECT COUNT(*) FROM jobs WHERE company_key = c.normalized_name AND active = 1) AS job_count
                    FROM companies c
                    LEFT JOIN company_scores s
                      ON s.id = (SELECT id FROM company_scores WHERE company_id = c.id ORDER BY created_at DESC, id DESC LIMIT 1)
                    ORDER BY score DESC, name
                    """
                ).fetchall()
            ]

    def unresearched_employers(self, query: str = "", limit: int = 12) -> dict[str, Any]:
        """Employers that post jobs but carry no company profile, busiest first.

        A source pack adds dozens of employers at once and research runs one company at a time,
        so the researched table is always the small end of the list. The page can only say what
        is missing if it can count it.
        """
        query = query.strip()
        where = """
            FROM jobs
            WHERE active = 1 AND company != ''
              AND company_key NOT IN (SELECT normalized_name FROM companies)
              AND (? = '' OR company LIKE ?)
        """
        parameters = (query, f"%{query}%")
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT MIN(company) AS name, COUNT(*) AS job_count {where} "
                "GROUP BY company_key ORDER BY job_count DESC, name LIMIT ?",
                (*parameters, limit),
            ).fetchall()
            total = int(connection.execute(f"SELECT COUNT(DISTINCT company_key) AS total {where}", parameters).fetchone()["total"])
        return {"employers": [dict(row) for row in rows], "total": total}

    def suggest_companies(self, prefix: str, limit: int = 20) -> list[str]:
        """Distinct employer names for autocomplete, drawn from the jobs table itself so every
        suggestion is guaranteed to actually match the company filter (which also matches jobs,
        not the much smaller researched-companies table)."""
        prefix = prefix.strip()
        if not prefix:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT company FROM jobs WHERE active = 1 AND company LIKE ? ORDER BY company LIMIT ?",
                (f"%{prefix}%", limit),
            ).fetchall()
            return [str(row["company"]) for row in rows]

    def dashboard_stats(self) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN julianday(first_seen_at) >= julianday('now', '-1 day') THEN 1 ELSE 0 END) AS new_today,
                    SUM(CASE WHEN score_status = 'pending_llm' THEN 1 ELSE 0 END) AS pending_llm,
                    SUM(CASE WHEN status = 'bookmarked' THEN 1 ELSE 0 END) AS shortlisted
                FROM jobs WHERE active = 1 AND merged_into_job_id IS NULL
                """
            ).fetchone()
            errors = connection.execute("SELECT COUNT(*) AS count FROM source_state WHERE status = 'error'").fetchone()
            result = dict(row)
            result["source_errors"] = int(errors["count"])
            return {key: int(value or 0) for key, value in result.items()}

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in (
            "metadata_json", "reasons_json", "risks_json", "dimensions_json",
            "evidence_json", "gaps_json", "requirements_json",
        ):
            if key in result and result[key]:
                result[key.removesuffix("_json")] = json.loads(result[key])
        return result
