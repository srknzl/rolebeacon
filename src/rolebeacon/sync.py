from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .collectors import as_batch, create_collector, default_http_client
from .config import Settings
from .database import Database
from .domain import CollectedJob, EligibilityStatus
from .llm import LlmClient, LlmResponseRejected, LlmUnavailable
from .scoring import (
    SCORING_PROMPT_VERSION,
    candidate_terms,
    evaluate_eligibility,
    extract_experience_requirements,
    rule_score,
)

# Different sources hit different providers, so syncing them concurrently is safe - these two
# caps just keep any one provider from being hammered hard enough to get rate-limited/blocked.
SOURCE_CONCURRENCY = 6
PER_KIND_CONCURRENCY = 2


def _friendly_error_prefix(error: Exception) -> str:
    """A short human sentence for the handful of httpx exceptions actually seen in practice,
    prefixed onto the existing raw exception text - not a general error-classification framework."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 429:
            return "The source is rate-limiting requests. "
        if 500 <= status < 600:
            return "The source's server had an error. "
        if 400 <= status < 500:
            return "The source rejected the request. "
    elif isinstance(error, httpx.TimeoutException):
        return "The source took too long to respond. "
    elif isinstance(error, httpx.ConnectError):
        return "Could not connect to the source. "
    return ""


@dataclass(slots=True)
class SyncStatus:
    running: bool = False
    started_at: str = ""
    finished_at: str = ""
    current_source: str = ""
    sources_completed: int = 0
    sources_total: int = 0
    jobs_seen: int = 0
    jobs_changed: int = 0
    jobs_scored: int = 0
    jobs_to_score: int = 0
    llm_available: bool = False
    llm_status: str = "rules_only"
    llm_mode: str = "rules"
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_error: str = ""
    phase: str = "idle"
    phase_message: str = "Ready"
    progress_percent: int = 0
    source_errors: int = 0
    rule_fallback_jobs: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SyncService:
    def __init__(self, settings: Settings, database: Database, llm: LlmClient):
        self.settings = settings
        self.database = database
        self.llm = llm
        self.status = SyncStatus()
        self._lock = asyncio.Lock()

    async def run(self, force: bool = False, manual: bool = False) -> SyncStatus:
        if self._lock.locked():
            return self.status
        async with self._lock:
            if not self.settings.setup_complete or (not manual and not self.settings.activated):
                self.status = SyncStatus(error="setup_required")
                return self.status
            self.database.reset_stale_sync_runs()
            sources = [source for source in self.settings.load_sources() if source.enabled]
            self.status = SyncStatus(
                running=True,
                started_at=datetime.now(UTC).isoformat(),
                sources_total=len(sources),
                llm_mode=self.settings.llm_mode,
                llm_endpoint=self.settings.llm_base_url if self.settings.llm_enabled else "",
                llm_model=self.settings.llm_model if self.settings.llm_enabled else "",
                phase="preparing",
                phase_message="Preparing sources and scoring configuration",
                progress_percent=2,
            )
            changed_ids: set[int] = set()
            search_profile = self.settings.load_search_profile()
            mobility_profile = self.settings.load_mobility_profile()
            strategies = self.settings.load_strategies()
            try:
                self.status.phase = "checking_model"
                self.status.phase_message = "Checking LLM availability"
                self.status.progress_percent = 8
                health = await self.llm.health()
                self.status.llm_available = bool(health["available"])
                self.status.llm_status = str(health["status"])
                self.status.llm_error = str(health["error"])
                if self.settings.llm_enabled and not self.status.llm_available:
                    self.status.error = (
                        "LLM unavailable: "
                        f"{self.status.llm_error or 'the configured endpoint did not provide the selected model'}. "
                        "Fix the model in Settings or explicitly choose Rules only, then refresh again."
                    )
                    return self.status
                self.status.phase = "collecting"
                self.status.phase_message = "Collecting job postings"
                self.status.progress_percent = 10
                async with default_http_client() as client:
                    global_gate = asyncio.Semaphore(SOURCE_CONCURRENCY)
                    kind_gates: dict[str, asyncio.Semaphore] = {}

                    async def guarded(source: Any) -> None:
                        gate = kind_gates.setdefault(source.kind, asyncio.Semaphore(PER_KIND_CONCURRENCY))
                        async with global_gate, gate:
                            await self._sync_one_source(source, client, search_profile, force, changed_ids)

                    await asyncio.gather(*(guarded(source) for source in sources))

                scoring_version = (
                    f"{SCORING_PROMPT_VERSION}:{self.settings.llm_model}"
                    if self.settings.llm_enabled
                    else f"{SCORING_PROMPT_VERSION}:rules"
                )
                # The 1000-job cap exists to bound an LLM-mode sync's wall-clock time; rules scoring
                # is local and fast enough to clear the whole backlog every run.
                score_limit = 1000 if self.settings.llm_enabled else 100_000
                pending = set(self.database.pending_job_ids(scoring_version, limit=score_limit)) | changed_ids
                self.status.jobs_to_score = len(pending)
                self.status.phase = "scoring"
                self.status.phase_message = "Ranking eligible jobs"
                self.status.progress_percent = 65
                candidate_profile = self.settings.load_candidate_profile()
                known_terms = candidate_terms(candidate_profile)
                for job_id in pending:
                    job_record = self.database.get_job(job_id)
                    if not job_record:
                        continue
                    eligibility = evaluate_eligibility(job_record, search_profile, mobility_profile, strategies)
                    rules = rule_score(job_record, eligibility, search_profile, candidate_profile, strategies)
                    requirements = extract_experience_requirements(str(job_record.get("description", "")), known_terms)
                    score = rules
                    score_status = "scored"
                    # The LLM only refines jobs rules already thinks are in the right role family
                    # (role_domain > 9 is the same "unrelated" boundary hide_mismatched_titles uses
                    # in database.py) - scoring the full firehose costs seconds per job for no
                    # benefit on titles rules can already tell are the wrong job.
                    if (
                        eligibility.status != EligibilityStatus.INELIGIBLE
                        and self.settings.llm_enabled
                        and rules.dimensions["role_domain"] > 9
                    ):
                        try:
                            score = await self.llm.score(job_record, eligibility, search_profile, candidate_profile)
                        except LlmResponseRejected as error:
                            # The model answered and its answer failed validation twice - keep the
                            # rules score already computed above rather than aborting the sync.
                            self.status.rule_fallback_jobs += 1
                            self.status.llm_error = f"Model response rejected, used rules score for job {job_id}: {error}"
                        except LlmUnavailable as error:
                            self.status.llm_available = False
                            self.status.llm_status = "unavailable"
                            self.status.llm_error = f"The model became unavailable while scoring: {error}"
                            raise LlmUnavailable(
                                f"LLM unavailable: {self.status.llm_error}. Fix the model in Settings or explicitly "
                                "choose Rules only, then refresh again."
                            ) from error
                    score.prompt_version = scoring_version
                    self.database.save_evaluation(job_id, eligibility, score, score_status, requirements=requirements)
                    self.status.jobs_scored += 1
                    self.status.progress_percent = 65 + int(30 * self.status.jobs_scored / max(1, self.status.jobs_to_score))
            except Exception as error:
                self.status.error = f"{type(error).__name__}: {error}"
            finally:
                self.status.running = False
                self.status.current_source = ""
                self.status.finished_at = datetime.now(UTC).isoformat()
                self.status.phase = "failed" if self.status.error else "complete"
                self.status.phase_message = self.status.error or "Refresh complete"
                self.status.progress_percent = 100
            return self.status

    async def _sync_one_source(
        self, source: Any, client: Any, search_profile: dict[str, Any], force: bool, changed_ids: set[int]
    ) -> None:
        self.status.current_source = source.name
        state = self.database.source_state(source.id) or {}
        skip_reason, next_eligible = self._skip_reason(source, state, force)
        run_started = datetime.now(UTC)
        run_id = self.database.start_sync_run(source.id)
        if skip_reason:
            self.database.skip_source(source.id, skip_reason, next_eligible)
            self.database.finish_sync_run(run_id, status="skipped", started_at=run_started, skip_reason=skip_reason)
            self.status.sources_completed += 1
            self.status.progress_percent = 10 + int(50 * self.status.sources_completed / max(1, self.status.sources_total))
            return
        self.database.start_source(source.id)
        since = self._since(state.get("last_successful_sync_at"))
        try:
            collector = create_collector(personalize_source(source, search_profile), client)
            batch = as_batch(await collector.collect(since, state.get("cursor", "")))
            raw_count = len(batch.jobs)
            jobs = deduplicate_source_jobs(batch.jobs)
            duplicate_count = raw_count - len(jobs)
            filtered = 0
            changed = 0
            created = 0
            for job in jobs:
                if batch.attribution:
                    job.metadata.setdefault("source_attribution", batch.attribution)
                if source.ingestion_filter and not engineering_job(job, search_profile=search_profile):
                    filtered += 1
                    continue
                existed = self.database.has_source_job(job.source, job.source_job_id)
                matched_job_id = self.database.matching_job_id(job)
                job_id, did_change = self.database.upsert_job(job, source.trust_priority)
                if matched_job_id is None:
                    created += 1
                elif not existed:
                    duplicate_count += 1
                if did_change:
                    changed_ids.add(job_id)
                    changed += 1
            self.database.finish_source(source.id, len(jobs), changed, batch.cursor)
            self.database.finish_sync_run(
                run_id, status="success", started_at=run_started, jobs_seen=len(jobs),
                jobs_new=created, jobs_changed=changed, jobs_filtered=filtered,
                duplicates=duplicate_count, requests_made=batch.requests_made,
            )
            self.status.jobs_seen += len(jobs) - filtered
            self.status.jobs_changed += changed
        except Exception as error:
            message = f"{_friendly_error_prefix(error)}{type(error).__name__}: {error}"
            self.database.fail_source(source.id, message)
            self.database.finish_sync_run(run_id, status="error", started_at=run_started, error=message)
            self.status.source_errors += 1
        finally:
            self.status.sources_completed += 1
            self.status.progress_percent = 10 + int(50 * self.status.sources_completed / max(1, self.status.sources_total))

    def _skip_reason(self, source: Any, state: dict[str, Any], force: bool) -> tuple[str, datetime | None]:
        now = datetime.now(UTC)
        retry_at = state.get("next_eligible_sync_at")
        if retry_at and state.get("status") == "error" and not force:
            try:
                retry_due = datetime.fromisoformat(retry_at.replace("Z", "+00:00")).astimezone(UTC)
                if retry_due > now:
                    return "retry_backoff", retry_due
            except ValueError:
                pass
        last_success = state.get("last_successful_sync_at")
        if not force and last_success and source.min_sync_interval_seconds > 0:
            try:
                parsed = datetime.fromisoformat(last_success.replace("Z", "+00:00")).astimezone(UTC)
                due = parsed + timedelta(seconds=source.min_sync_interval_seconds)
                if due > now:
                    return "minimum_sync_interval", due
            except ValueError:
                pass
        keyed = {
            "adzuna": ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"),
            "jooble": ("JOOBLE_API_KEY",),
            "serpapi": ("SERPAPI_API_KEY",),
        }
        if source.kind in keyed:
            if not all(os.getenv(name) for name in keyed[source.kind]):
                return "credentials_not_configured", None
            if source.monthly_request_limit <= 0:
                return "monthly_request_limit_is_zero", None
            if not self.database.reserve_api_requests(
                source.kind, max(1, source.request_budget_cost), source.monthly_request_limit
            ):
                return "monthly_request_budget_exhausted", None
        return "", None

    def _since(self, last_successful: str | None) -> datetime:
        now = datetime.now(UTC)
        if not last_successful:
            return now - timedelta(days=self.settings.initial_lookback_days)
        try:
            parsed = datetime.fromisoformat(last_successful.replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
        except ValueError:
            return now - timedelta(days=self.settings.initial_lookback_days)
        return parsed - timedelta(hours=self.settings.overlap_hours)


def deduplicate_source_jobs(jobs: list[CollectedJob]) -> list[CollectedJob]:
    """Keep one stable update per source identity within a collector run."""
    unique: dict[tuple[str, str], CollectedJob] = {}
    for job in jobs:
        unique[(job.source, job.source_job_id)] = job
    return list(unique.values())


def _profile_terms(search_profile: dict[str, Any]) -> set[str]:
    """Terms the candidate explicitly supplied for role-family ingestion decisions."""
    terms: set[str] = set()
    for role in search_profile.get("target_roles", []):
        terms.update(token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z+#.-]{2,}", str(role)))
    for domain in search_profile.get("preferred_domains", []):
        terms.update(token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z+#.-]{2,}", str(domain)))
    terms.update(
        str(skill).strip().casefold()
        for skill in search_profile.get("preferred_skills", [])
        if str(skill).strip()
    )
    return {term for term in terms if term}


def engineering_job(job: CollectedJob, search_profile: dict[str, Any]) -> bool:
    watchlist = [
        *search_profile.get("priority_companies", []),
        *search_profile.get("company_watchlist", []),
    ]
    if any(company.casefold() == job.company.casefold() for company in watchlist):
        return True
    categories = " ".join(map(str, job.metadata.get("categories", [])))
    searchable = f"{job.title} {categories}".casefold()
    if any(
        str(role).strip().casefold() in searchable
        for role in search_profile.get("target_roles", [])
        if str(role).strip()
    ):
        return True
    terms = _profile_terms(search_profile)
    return not terms or any(
        re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", searchable)
        for term in terms
    )


_URL_QUERY_KINDS = {"google_careers": "q", "amazon_jobs": "base_query"}
_OPTION_QUERY_KINDS = {"adzuna": "query", "jooble": "query", "serpapi": "query", "remotive": "search"}


def personalize_source(source: Any, search_profile: dict[str, Any]) -> Any:
    """Refresh each provider's free-text role query from the candidate's real target_roles.

    Location is intentionally not touched here: relocation_source_candidates() bakes the correct
    per-country location into each generated Google/Amazon row once, and every Setup/Settings save
    re-bakes it (save_sources() replaces a matched row's url/options wholesale). Overwriting it again
    here from "the first relocation target" would collapse every generated per-country row back onto
    the same one country on every sync - which is exactly the bug this replaced.
    """
    roles = [str(value).strip() for value in search_profile.get("target_roles", []) if str(value).strip()]
    role_query = " OR ".join(roles[:5])
    if not role_query:
        return source
    if source.kind in _URL_QUERY_KINDS and source.url:
        key = _URL_QUERY_KINDS[source.kind]
        parts = urlsplit(source.url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query[key] = role_query
        return replace(source, url=urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)))
    if source.kind in _OPTION_QUERY_KINDS:
        return replace(source, options={**source.options, _OPTION_QUERY_KINDS[source.kind]: role_query})
    return source


class Scheduler:
    def __init__(self, sync_service: SyncService, interval_seconds: int):
        self.sync_service = sync_service
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self, run_immediately: bool = True) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(run_immediately))

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self, run_immediately: bool) -> None:
        if run_immediately:
            await self.sync_service.run()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                await self.sync_service.run()
