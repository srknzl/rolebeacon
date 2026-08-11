from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    UNKNOWN = "unknown"
    INELIGIBLE = "ineligible"


class JobStatus(StrEnum):
    NEW = "new"
    INTERESTED = "interested"
    MAYBE = "maybe"
    REJECTED = "rejected"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"


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


@dataclass(slots=True)
class EligibilityResult:
    status: EligibilityStatus
    route: str
    sponsorship: str
    relocation: str
    location_fit: str
    reasons: list[str]
    risks: list[str]


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
