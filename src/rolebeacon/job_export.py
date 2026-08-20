from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .database import Database

EXPORT_SCHEMA_VERSION = "1.0"
RECOMMENDED_MIN_SCORE = 65


@dataclass(frozen=True, slots=True)
class JobExportResult:
    directory: Path
    all_jobs_count: int
    recommended_jobs_count: int
    paths: tuple[Path, ...]


def _is_recommended(job: dict[str, Any]) -> bool:
    return int(job.get("score") or 0) >= RECOMMENDED_MIN_SCORE and job.get("eligibility_status") != "ineligible"


def _project_job(
    job: dict[str, Any], sources: list[dict[str, Any]], *, recommended: bool, source_name: str = ""
) -> dict[str, Any]:
    posting_url = str(job.get("canonical_url") or "")
    application_url = str(job.get("apply_url") or posting_url)
    return {
        "id": int(job["id"]),
        "recommended": recommended,
        "title": str(job.get("title") or ""),
        "company": str(job.get("company") or ""),
        "location": str(job.get("location") or ""),
        "description": str(job.get("description") or ""),
        "posting_url": posting_url,
        "application_url": application_url,
        "remote_scope": str(job.get("remote_scope") or ""),
        "employment_type": str(job.get("employment_type") or ""),
        "salary": {
            "minimum": job.get("salary_min"),
            "maximum": job.get("salary_max"),
            "currency": str(job.get("salary_currency") or ""),
        },
        "published_at": job.get("published_at"),
        "updated_at": job.get("updated_at"),
        "first_seen_at": job.get("first_seen_at"),
        "last_seen_at": job.get("last_seen_at"),
        "pipeline_status": str(job.get("status") or "new"),
        "scoring_status": str(job.get("score_status") or "pending"),
        "primary_source_id": str(job.get("primary_source_id") or ""),
        # The id is what a machine joins on; the name is what the person reading the export knows
        # the source by, and `linkedin-t-rkiye` is not a name.
        "primary_source_name": source_name or str(job.get("primary_source_id") or ""),
        "sources": sources,
        "metadata": job.get("metadata") or {},
        "requirements": job.get("requirements") or [],
        "eligibility": {
            "status": str(job.get("eligibility_status") or "unknown"),
            "route": str(job.get("route") or ""),
            "sponsorship": str(job.get("sponsorship") or "unknown"),
            "relocation": str(job.get("relocation") or "unknown"),
            "location_fit": str(job.get("location_fit") or ""),
            "reasons": job.get("reasons") or [],
            "risks": job.get("risks") or [],
        },
        "scoring": {
            "job_fit": job.get("score"),
            "company_fit": job.get("company_score"),
            "opportunity": job.get("opportunity_score"),
            "verdict": str(job.get("verdict") or ""),
            "dimensions": job.get("dimensions") or {},
            "confidence": job.get("confidence"),
            "provider": str(job.get("provider") or ""),
            "model": str(job.get("model") or ""),
            "evidence": job.get("evidence") or [],
            "gaps": job.get("gaps") or [],
        },
    }


def _envelope(
    *,
    kind: str,
    generated_at: str,
    sync: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    selection: dict[str, Any] = {
        "active_only": True,
        "exclude_merged": True,
        "sort": "decision_ready",
    }
    if kind == "recommended":
        selection.update(
            minimum_job_fit_score=RECOMMENDED_MIN_SCORE,
            exclude_eligibility_status="ineligible",
        )
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "kind": kind,
        "generated_at": generated_at,
        "selection": selection,
        "sync": sync,
        "count": len(jobs),
        "jobs": jobs,
    }


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _markdown(envelope: dict[str, Any]) -> str:
    kind = "Recommended jobs" if envelope["kind"] == "recommended" else "All jobs"
    selection = envelope["selection"]
    criteria = "Job-fit score ≥65 and eligibility is not ineligible." if envelope["kind"] == "recommended" else "All active, unmerged jobs."
    # Every row of the recommended export is recommended, so the column would say "yes" 2,893 times.
    show_recommended = envelope["kind"] != "recommended"
    lines = [
        f"# {kind}",
        "",
        f"Generated: {_local_stamp(envelope['generated_at'])}",
        f"Count: {envelope['count']}",
        f"Selection: {criteria} Sorted by `{selection['sort']}`.",
        "",
        "| Rank |" + (" Recommended |" if show_recommended else "")
        + " Job fit | Eligibility | Title | Company | Location | Posted | Source | Link |",
        "| ---: |" + (" :---: |" if show_recommended else "")
        + " ---: | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rank, job in enumerate(envelope["jobs"], start=1):
        url = _markdown_cell(job["application_url"] or job["posting_url"])
        link = f"[Open]({url})" if url else ""
        lines.append(
            "| "
            + " | ".join(
                (
                    str(rank),
                    *(("yes" if job["recommended"] else "",) if show_recommended else ()),
                    _markdown_cell(job["scoring"]["job_fit"]),
                    _markdown_cell(job["eligibility"]["status"]),
                    _markdown_cell(job["title"]),
                    _markdown_cell(job["company"]),
                    _markdown_cell(job["location"]),
                    _markdown_cell(str(job["published_at"] or job["first_seen_at"] or "")[:10]),
                    _markdown_cell(job["primary_source_name"]),
                    link,
                )
            )
            + " |"
        )
    if not envelope["jobs"]:
        lines.extend(("", "_No jobs matched this export._"))
    return "\n".join(lines) + "\n"


def _local_stamp(generated_at: str) -> str:
    """The moment a person reads: local time, to the minute, with the zone named.

    The machine-readable UTC instant stays in the JSON envelope's `generated_at`.
    """
    return datetime.fromisoformat(generated_at).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _destination(base_directory: Path, generated_at: datetime) -> Path:
    timestamp = generated_at.astimezone().strftime("%Y-%m-%d-%H%M")
    candidate = base_directory / f"rolebeacon-jobs-{timestamp}"
    suffix = 2
    while candidate.exists():
        candidate = base_directory / f"rolebeacon-jobs-{timestamp}-{suffix}"
        suffix += 1
    return candidate


def export_jobs(
    database: Database,
    base_directory: Path,
    *,
    sync: dict[str, Any],
    generated_at: datetime | None = None,
    source_names: Mapping[str, str] | None = None,
) -> JobExportResult:
    generated = generated_at or datetime.now(UTC)
    generated_iso = generated.astimezone(UTC).isoformat()
    rows = database.list_jobs(sort="decision_ready", limit=None)
    sources = database.list_job_sources([int(row["id"]) for row in rows])
    names = source_names or {}
    projected = [
        _project_job(
            row,
            sources.get(int(row["id"]), []),
            recommended=_is_recommended(row),
            source_name=names.get(str(row.get("primary_source_id") or ""), ""),
        )
        for row in rows
    ]
    recommended = [job for job in projected if job["recommended"]]
    all_envelope = _envelope(kind="all", generated_at=generated_iso, sync=sync, jobs=projected)
    recommended_envelope = _envelope(
        kind="recommended", generated_at=generated_iso, sync=sync, jobs=recommended
    )
    contents = {
        "recommended-jobs.json": json.dumps(recommended_envelope, ensure_ascii=False, indent=2) + "\n",
        "recommended-jobs.md": _markdown(recommended_envelope),
        "all-jobs.json": json.dumps(all_envelope, ensure_ascii=False, indent=2) + "\n",
        "all-jobs.md": _markdown(all_envelope),
    }

    base = base_directory.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    destination = _destination(base, generated)
    temporary = Path(tempfile.mkdtemp(prefix=".rolebeacon-jobs-", dir=base))
    try:
        for name, content in contents.items():
            (temporary / name).write_text(content, encoding="utf-8")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    paths = tuple(destination / name for name in contents)
    return JobExportResult(
        directory=destination,
        all_jobs_count=len(projected),
        recommended_jobs_count=len(recommended),
        paths=paths,
    )
