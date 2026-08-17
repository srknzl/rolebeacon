from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rolebeacon.database import Database
from rolebeacon.domain import CollectedJob, EligibilityResult, EligibilityStatus, ScoreResult
from rolebeacon.job_export import export_jobs


def _add_scored_job(
    database: Database,
    label: str,
    score: int,
    eligibility: EligibilityStatus,
    *,
    company: str | None = None,
) -> int:
    job_id, _ = database.upsert_job(
        CollectedJob(
            source=f"source-{label}",
            source_job_id=label,
            title=f"{label.title()} Backend Engineer",
            company=company or f"Company {label}",
            location="Remote | Türkiye",
            description=f"Build distributed systems for {label}",
            url=f"https://example.com/jobs/{label}",
            apply_url=f"https://example.com/jobs/{label}/apply",
            published_at=datetime(2026, 8, 10, tzinfo=UTC),
            metadata={"public_note": f"source metadata {label}", "job_req_id": label},
        )
    )
    database.save_evaluation(
        job_id,
        EligibilityResult(
            status=eligibility,
            route="remote-from-tr",
            sponsorship="unknown",
            relocation="unknown",
            location_fit="authorized:TR",
            reasons=["Location is supported"],
            risks=["Sponsorship is unknown"],
        ),
        ScoreResult(
            total=score,
            dimensions={
                "role_domain": min(score, 30),
                "stack": 20,
                "domain_experience": 10,
                "seniority": 5,
                "location_authorization": 0,
                "salary_employment": 0,
            },
            confidence=0.8,
            verdict="review" if score >= 65 else "low_priority",
            evidence=[{"requirement": "Backend", "profile_evidence": "Backend experience"}],
            gaps=[{"requirement": "Rust", "severity": "low"}],
            provider="rules",
            model="deterministic",
        ),
        "scored",
        requirements=[{"requirement": "5 years", "unmet": False}],
    )
    return job_id


def test_export_is_complete_versioned_and_dashboard_compatible(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    eligible_id = _add_scored_job(
        database, "eligible", 65, EligibilityStatus.ELIGIBLE, company="Çağrı | Labs"
    )
    _add_scored_job(database, "low", 64, EligibilityStatus.ELIGIBLE)
    _add_scored_job(database, "unknown", 90, EligibilityStatus.UNKNOWN)
    _add_scored_job(database, "ineligible", 99, EligibilityStatus.INELIGIBLE)

    duplicate_source = CollectedJob(
        source="secondary",
        source_job_id="secondary-eligible",
        title="Eligible Backend Engineer",
        company="Çağrı | Labs",
        location="Remote | Türkiye",
        description="Build distributed systems for eligible",
        url="https://secondary.example/jobs/eligible",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        metadata={"job_req_id": "eligible"},
    )
    duplicate_id, _ = database.upsert_job(duplicate_source, source_priority=40)
    assert duplicate_id == eligible_id

    for index in range(101):
        database.upsert_job(
            CollectedJob(
                source="bulk",
                source_job_id=f"bulk-{index}",
                title=f"Unscored Role {index}",
                company=f"Bulk Company {index}",
                location="Unknown",
                description="",
                url=f"https://bulk.example/jobs/{index}",
            )
        )

    generated = datetime(2026, 8, 17, 12, 30, 45, 123456, tzinfo=UTC)
    result = export_jobs(
        database,
        tmp_path / "exports",
        sync={"requested": False, "performed": False, "status": None},
        generated_at=generated,
    )

    assert result.all_jobs_count == 105
    assert result.recommended_jobs_count == 2
    assert result.directory.name == "rolebeacon-jobs-20260817T123045123456Z"
    assert {path.name for path in result.paths} == {
        "recommended-jobs.json",
        "recommended-jobs.md",
        "all-jobs.json",
        "all-jobs.md",
    }

    all_export = json.loads((result.directory / "all-jobs.json").read_text(encoding="utf-8"))
    recommended = json.loads((result.directory / "recommended-jobs.json").read_text(encoding="utf-8"))
    assert all_export["schema_version"] == "1.0"
    assert all_export["count"] == 105
    assert [job["eligibility"]["status"] for job in recommended["jobs"]] == ["eligible", "unknown"]
    assert recommended["selection"] == {
        "active_only": True,
        "exclude_merged": True,
        "sort": "decision_ready",
        "minimum_job_fit_score": 65,
        "exclude_eligibility_status": "ineligible",
    }
    eligible = next(job for job in all_export["jobs"] if job["id"] == eligible_id)
    assert eligible["description"] == "Build distributed systems for eligible"
    assert eligible["application_url"].endswith("/apply")
    assert [source["source_id"] for source in eligible["sources"]] == ["source-eligible", "secondary"]
    assert eligible["sources"][0]["metadata"] == {
        "public_note": "source metadata eligible",
        "job_req_id": "eligible",
    }
    assert eligible["eligibility"]["reasons"] == ["Location is supported"]
    assert eligible["scoring"]["job_fit"] == 65
    assert eligible["requirements"] == [{"requirement": "5 years", "unmet": False}]
    assert not {
        "fingerprint",
        "content_hash",
        "company_key",
        "metadata_json",
        "dimensions_json",
        "candidate",
    } & eligible.keys()
    assert "Çağrı \\| Labs" in (result.directory / "all-jobs.md").read_text(encoding="utf-8")


def test_export_never_overwrites_a_run_with_the_same_timestamp(tmp_path) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    generated = datetime(2026, 8, 17, tzinfo=UTC)
    sync = {"requested": False, "performed": False, "status": None}

    first = export_jobs(database, tmp_path, sync=sync, generated_at=generated)
    second = export_jobs(database, tmp_path, sync=sync, generated_at=generated)

    assert first.directory.name == "rolebeacon-jobs-20260817T000000000000Z"
    assert second.directory.name == "rolebeacon-jobs-20260817T000000000000Z-2"
    assert (first.directory / "all-jobs.json").exists()
    assert (second.directory / "all-jobs.json").exists()


def test_export_cleans_temporary_directory_when_publication_fails(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    original = Path.write_text

    def fail_on_last_file(path: Path, content: str, *, encoding: str) -> int:
        if path.name == "all-jobs.md":
            raise OSError("disk full")
        return original(path, content, encoding=encoding)

    monkeypatch.setattr(Path, "write_text", fail_on_last_file)

    with pytest.raises(OSError, match="disk full"):
        export_jobs(
            database,
            tmp_path / "exports",
            sync={"requested": False, "performed": False, "status": None},
        )

    assert not list((tmp_path / "exports").iterdir())


def test_interrupted_staging_directories_are_gitignored() -> None:
    ignore = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "rolebeacon-jobs-*/" in ignore
    assert ".rolebeacon-jobs-*/" in ignore
