from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .domain import CollectedJob, EligibilityResult, JobStatus, ScoreResult


@dataclass(slots=True)
class JobFilters:
    """Every way the job list can be narrowed. Filters narrow the set; `sort` only reorders it."""

    query: str = ""
    title: str = ""
    technologies: tuple[str, ...] = ()
    route: str = ""
    status: str = ""
    source: str = ""
    company: str = ""
    eligibility: str = ""
    sponsorship: str = ""
    relocation: str = ""
    work_model: str = ""
    seniority: str = ""
    provider: str = ""
    posted_within_days: int = 0
    min_score: int = 0
    min_title_match: int = 0
    min_stack_match: int = 0
    salary_floor: float = 0
    has_salary: bool = False
    exclude_ineligible: bool = False


# Artifact preparation stages, in the only order they may advance.
ARTIFACT_STAGES = ("saved", "preparing", "ready")
# Outcomes the user records on the job itself. They own the pipeline column once set.
APPLICATION_OUTCOMES = ("applied", "interview", "offer", "rejected")

# Sort keys are a fixed allow-list because they are interpolated into ORDER BY.
JOB_SORTS: dict[str, str] = {
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
    location_bucket TEXT NOT NULL DEFAULT ''
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
    last_skipped_reason TEXT NOT NULL DEFAULT ''
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
    skip_reason TEXT NOT NULL DEFAULT ''
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
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def company_key(name: str) -> str:
    value = name.casefold()
    value = re.sub(r"\b(?:incorporated|corporation|company|limited|ltd|llc|inc|gmbh|ag|se)\b", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


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
            for name, definition in (
                ("source_priority", "INTEGER NOT NULL DEFAULT 50"),
                ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("content_hash", "TEXT NOT NULL DEFAULT ''"),
                ("active", "INTEGER NOT NULL DEFAULT 1"),
            ):
                self._ensure_column(connection, "job_sources", name, definition)
            self._ensure_column(connection, "source_state", "next_eligible_sync_at", "TEXT")
            self._ensure_column(connection, "source_state", "last_skipped_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "company_evidence", "etag", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "company_evidence", "last_modified", "TEXT NOT NULL DEFAULT ''")
            for row in connection.execute("SELECT id, company, title, location FROM jobs").fetchall():
                connection.execute(
                    "UPDATE jobs SET company_key = ?, normalized_title = ?, location_bucket = ? WHERE id = ?",
                    (company_key(row["company"]), normalized_title(row["title"]), location_bucket(row["location"]), row["id"]),
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

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def upsert_job(self, job: CollectedJob, source_priority: int = 50) -> tuple[int, bool]:
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
                        canonical, fingerprint, job.title, job.company, company_key(job.company), job.location, job.description,
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
        for candidate in candidates:
            if self._dates_compatible(candidate["published_at"], job.published_at) and not self._requisition_conflict(candidate, job):
                return candidate
        return None

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
                """
                SELECT j.*, e.status AS eligibility_status, e.route, e.sponsorship, e.relocation,
                       e.location_fit, e.reasons_json, e.risks_json,
                       ms.total AS score, ms.dimensions_json, ms.confidence, ms.verdict,
                       ms.evidence_json, ms.gaps_json, ms.provider, ms.model,
                       c.id AS company_id, c.remote_policy AS company_remote_policy,
                       c.sponsorship AS company_sponsorship, c.relocation AS company_relocation,
                       cs.total AS company_score,
                       CASE WHEN ms.total IS NULL THEN NULL WHEN cs.total IS NULL THEN ms.total
                            ELSE CAST(ROUND(ms.total * 0.8 + cs.total * 0.2) AS INTEGER) END AS opportunity_score
                FROM jobs j
                LEFT JOIN eligibility e ON e.job_id = j.id
                LEFT JOIN match_scores ms ON ms.id = (
                    SELECT id FROM match_scores WHERE job_id = j.id ORDER BY created_at DESC, id DESC LIMIT 1
                )
                LEFT JOIN companies c ON c.normalized_name = j.company_key
                LEFT JOIN company_scores cs ON cs.id = (
                    SELECT id FROM company_scores WHERE company_id = c.id ORDER BY created_at DESC, id DESC LIMIT 1
                )
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
        clauses = ["j.active = 1", "j.merged_into_job_id IS NULL", "COALESCE(ms.total, 0) >= ?"]
        params: list[Any] = [filters.min_score]
        if filters.route:
            clauses.append("e.route = ?")
            params.append(filters.route)
        if filters.status:
            clauses.append("j.status = ?")
            params.append(filters.status)
        if filters.source:
            clauses.append("EXISTS (SELECT 1 FROM job_sources js WHERE js.job_id = j.id AND js.source_id = ?)")
            params.append(filters.source)
        if filters.exclude_ineligible:
            clauses.append("COALESCE(e.status, 'unknown') <> 'ineligible'")
        if filters.eligibility:
            clauses.append("COALESCE(e.status, 'unknown') = ?")
            params.append(filters.eligibility)
        if filters.sponsorship:
            clauses.append("COALESCE(e.sponsorship, 'unknown') = ?")
            params.append(filters.sponsorship)
        if filters.relocation:
            clauses.append("COALESCE(e.relocation, 'unknown') = ?")
            params.append(filters.relocation)
        if filters.work_model == "remote_worldwide":
            clauses.append("j.location_bucket = 'remote:worldwide'")
        elif filters.work_model == "remote":
            clauses.append("j.location_bucket LIKE 'remote:%'")
        elif filters.work_model == "onsite":
            clauses.append("j.location_bucket NOT LIKE 'remote:%'")
        if filters.seniority:
            clauses.append("j.normalized_title LIKE ?")
            params.append(f"%{filters.seniority}%")
        if filters.title:
            clauses.append("j.normalized_title LIKE ?")
            params.append(f"%{filters.title.casefold()}%")
        for technology in filters.technologies:
            clauses.append("(j.title LIKE ? OR j.description LIKE ?)")
            params.extend([f"%{technology}%", f"%{technology}%"])
        if filters.company:
            clauses.append("j.company_key = ?")
            params.append(company_key(filters.company))
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
        if filters.provider:
            clauses.append("COALESCE(ms.provider, '') = ?")
            params.append(filters.provider)
        join_fts = ""
        if filters.query:
            join_fts = "JOIN jobs_fts f ON f.rowid = j.id"
            clauses.append("jobs_fts MATCH ?")
            params.append(filters.query)
        return clauses, params, join_fts

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
            WHERE {' AND '.join(clauses)}
        """
        with self.connect() as connection:
            return int(connection.execute(sql, params).fetchone()["total"])

    def list_jobs(
        self,
        filters: JobFilters | None = None,
        *,
        sort: str = "opportunity",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params, join_fts = self._job_filters(filters or JobFilters())
        params.extend((limit, offset))
        sql = f"""
            SELECT j.*, e.status AS eligibility_status, e.route, e.sponsorship, e.relocation,
                   e.location_fit, e.reasons_json, e.risks_json,
                   ms.total AS score, ms.dimensions_json, ms.confidence, ms.verdict,
                   ms.evidence_json, ms.gaps_json, ms.provider, ms.model,
                   c.id AS company_id, c.remote_policy AS company_remote_policy,
                   c.sponsorship AS company_sponsorship, c.relocation AS company_relocation,
                   cs.total AS company_score,
                   CASE WHEN ms.total IS NULL THEN NULL WHEN cs.total IS NULL THEN ms.total
                        ELSE CAST(ROUND(ms.total * 0.8 + cs.total * 0.2) AS INTEGER) END AS opportunity_score
            FROM jobs j
            {join_fts}
            LEFT JOIN eligibility e ON e.job_id = j.id
            LEFT JOIN match_scores ms ON ms.id = (
                SELECT id FROM match_scores WHERE job_id = j.id ORDER BY created_at DESC, id DESC LIMIT 1
            )
            LEFT JOIN companies c ON c.normalized_name = j.company_key
            LEFT JOIN company_scores cs ON cs.id = (
                SELECT id FROM company_scores WHERE company_id = c.id ORDER BY created_at DESC, id DESC LIMIT 1
            )
            WHERE {' AND '.join(clauses)}
            ORDER BY {JOB_SORTS.get(sort) or JOB_SORTS["opportunity"]},
                     COALESCE(opportunity_score, ms.total, 0) DESC,
                     COALESCE(j.published_at, j.first_seen_at) DESC
            LIMIT ? OFFSET ?
        """
        with self.connect() as connection:
            return [self._decode_row(row) for row in connection.execute(sql, params).fetchall()]

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

    def save_evaluation(self, job_id: int, eligibility: EligibilityResult, score: ScoreResult, status: str) -> None:
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
            connection.execute("UPDATE jobs SET score_status = ? WHERE id = ?", (status, job_id))

    def save_feedback(self, job_id: int, status: JobStatus, reason: str = "") -> None:
        now = _iso()
        with self.connect() as connection:
            connection.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job_id))
            connection.execute(
                "INSERT INTO feedback (job_id, status, reason, created_at) VALUES (?, ?, ?, ?)",
                (job_id, status.value, reason.strip(), now),
            )
            if status.value in APPLICATION_OUTCOMES:
                # An outcome can be recorded on a job that was applied to without RoleBeacon preparing
                # anything. Without a row here the pipeline board would never show it.
                connection.execute(
                    """
                    INSERT INTO applications (job_id, status, created_at, updated_at)
                    VALUES (?, 'saved', ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET updated_at = excluded.updated_at
                    """,
                    (job_id, now, now),
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
            connection.execute(
                f"""
                INSERT INTO applications (
                    job_id, status, resume_path, cover_letter_path, packet_path, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = CASE
                        WHEN {self._artifact_stage('excluded.status')} > {self._artifact_stage('applications.status')}
                        THEN excluded.status ELSE applications.status END,
                    resume_path = CASE WHEN excluded.resume_path <> '' THEN excluded.resume_path ELSE applications.resume_path END,
                    cover_letter_path = CASE WHEN excluded.cover_letter_path <> '' THEN excluded.cover_letter_path ELSE applications.cover_letter_path END,
                    packet_path = CASE WHEN excluded.packet_path <> '' THEN excluded.packet_path ELSE applications.packet_path END,
                    notes = CASE WHEN excluded.notes <> '' THEN excluded.notes ELSE applications.notes END,
                    updated_at = excluded.updated_at
                """,
                (job_id, status, resume_path or "", cover_letter_path or "", packet_path or "", notes or "", now, now),
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
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_state (source_id, status, last_started_at)
                VALUES (?, 'running', ?)
                ON CONFLICT(source_id) DO UPDATE SET status = 'running', last_started_at = excluded.last_started_at, last_error = ''
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

    def finish_sync_run(self, run_id: int, *, status: str, started_at: datetime, **metrics: Any) -> None:
        duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE source_sync_runs SET finished_at = ?, status = ?, jobs_seen = ?, jobs_new = ?,
                    jobs_changed = ?, jobs_filtered = ?, duplicates = ?, requests_made = ?,
                    duration_ms = ?, error = ?, skip_reason = ? WHERE id = ?
                """,
                (
                    _iso(), status, int(metrics.get("jobs_seen", 0)), int(metrics.get("jobs_new", 0)),
                    int(metrics.get("jobs_changed", 0)), int(metrics.get("jobs_filtered", 0)),
                    int(metrics.get("duplicates", 0)), int(metrics.get("requests_made", 0)), duration_ms,
                    str(metrics.get("error", ""))[:1000], str(metrics.get("skip_reason", ""))[:500], run_id,
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

    def finish_source(self, source_id: str, seen: int, changed: int, cursor: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_state (
                    source_id, status, last_started_at, last_successful_sync_at, cursor, jobs_seen, jobs_changed
                ) VALUES (?, 'idle', ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    status = 'idle', last_successful_sync_at = excluded.last_successful_sync_at,
                    last_error = '', cursor = excluded.cursor, jobs_seen = excluded.jobs_seen,
                    jobs_changed = excluded.jobs_changed, next_eligible_sync_at = NULL,
                    last_skipped_reason = ''
                """,
                (source_id, _iso(), _iso(), cursor, seen, changed),
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
            connection.execute("UPDATE job_sources SET job_id = ? WHERE job_id = ?", (winner, loser))
            connection.execute("UPDATE feedback SET job_id = ? WHERE job_id = ?", (winner, loser))
            loser_application = connection.execute("SELECT * FROM applications WHERE job_id = ?", (loser,)).fetchone()
            winner_application = connection.execute("SELECT id FROM applications WHERE job_id = ?", (winner,)).fetchone()
            if loser_application and not winner_application:
                connection.execute("UPDATE applications SET job_id = ? WHERE job_id = ?", (winner, loser))
            elif loser_application:
                connection.execute("DELETE FROM applications WHERE job_id = ?", (loser,))
            connection.execute("UPDATE jobs SET active = 0, merged_into_job_id = ? WHERE id = ?", (winner, loser))
            connection.execute(
                "UPDATE duplicate_candidates SET status = 'merged', decided_at = ? WHERE id = ?", (_iso(), candidate_id)
            )
            return winner

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
            return result

    def list_companies(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT c.*, (SELECT total FROM company_scores WHERE company_id = c.id ORDER BY created_at DESC, id DESC LIMIT 1) AS score,
                           (SELECT COUNT(*) FROM company_evidence WHERE company_id = c.id) AS evidence_count,
                           (SELECT COUNT(*) FROM jobs WHERE company_key = c.normalized_name AND active = 1) AS job_count
                    FROM companies c ORDER BY score DESC, name
                    """
                ).fetchall()
            ]

    def dashboard_stats(self) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN julianday(first_seen_at) >= julianday('now', '-1 day') THEN 1 ELSE 0 END) AS new_today,
                    SUM(CASE WHEN score_status = 'pending_llm' THEN 1 ELSE 0 END) AS pending_llm,
                    SUM(CASE WHEN status IN ('interested', 'maybe') THEN 1 ELSE 0 END) AS shortlisted
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
            "evidence_json", "gaps_json",
        ):
            if key in result and result[key]:
                result[key.removesuffix("_json")] = json.loads(result[key])
        return result
