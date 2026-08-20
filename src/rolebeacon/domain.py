from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def time_ago(value: str) -> str:
    """How long ago something happened, in the words a reader actually uses.

    An ISO timestamp answers "which instant"; a job seeker is asking "is this still open" and
    someone reading source health is asking "did this run recently". Below a day the answer is
    hours or minutes, which is the difference between a source that just ran and one that has
    been quiet all day. The exact timestamp stays in the element's title.
    """
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        return f"{days // 30} month{'s' if days // 30 > 1 else ''} ago"
    return moment.date().isoformat()


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    UNKNOWN = "unknown"
    INELIGIBLE = "ineligible"


class JobStatus(StrEnum):
    NEW = "new"                        # internal default; never a kanban column
    BOOKMARKED = "bookmarked"          # was: interested, maybe
    NOT_INTERESTED = "not_interested"  # was: rejected (pre-application meaning)
    APPLIED = "applied"                # was: applied, interview
    OFFER = "offer"
    REJECTED = "rejected"              # employer rejected post-application (new meaning)


@dataclass(slots=True)
class SourceConfig:
    id: str
    kind: str
    name: str
    enabled: bool = True
    company: str = ""
    slug: str = ""
    url: str = ""
    host: str = ""
    tenant: str = ""
    site: str = ""
    min_sync_interval_seconds: int = 0
    trust_priority: int = 50
    max_pages: int = 20
    monthly_request_limit: int = 0
    request_budget_cost: int = 1
    ingestion_filter: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceConfig:
        defaults: dict[str, Any] = {
            "id": "", "kind": "", "name": "", "enabled": True, "company": "", "slug": "", "url": "",
            "host": "", "tenant": "", "site": "", "min_sync_interval_seconds": 0, "trust_priority": 50,
            "max_pages": 20, "monthly_request_limit": 0, "request_budget_cost": 1, "ingestion_filter": False,
        }
        known = {key: value.get(key, default) for key, default in defaults.items()}
        known["options"] = {
            key: item
            for key, item in value.items()
            if key not in known
        }
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        options = value.pop("options")
        value.update(options)
        return value


@dataclass(slots=True)
class CollectedJob:
    source: str
    source_job_id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    apply_url: str = ""
    remote_scope: str = ""
    employment_type: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    published_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("published_at", "updated_at"):
            value = result[key]
            result[key] = value.isoformat() if value else None
        return result


@dataclass(slots=True)
class CollectionBatch:
    jobs: list[CollectedJob]
    cursor: str = ""
    complete_snapshot: bool = False
    provider_total: int | None = None
    requests_made: int = 1
    attribution: str = ""
    truncated: bool = False

    def __iter__(self) -> Iterator[CollectedJob]:
        return iter(self.jobs)

    def __getitem__(self, index: int) -> CollectedJob:
        return self.jobs[index]

    def __len__(self) -> int:
        return len(self.jobs)


@dataclass(slots=True)
class EligibilityResult:
    status: EligibilityStatus
    route: str
    sponsorship: str
    relocation: str
    location_fit: str
    reasons: list[str]
    risks: list[str]
    # The score a job needs to earn a "review" verdict, from the governing strategy's own
    # "threshold" (rule_score and llm.py's _normalize_score both read this - one verdict rule).
    threshold: int = 80


@dataclass(slots=True)
class ScoreResult:
    total: int
    dimensions: dict[str, int]
    confidence: float
    verdict: str
    evidence: list[dict[str, str]]
    gaps: list[dict[str, str]]
    provider: str
    model: str
    prompt_version: str = "v1"
