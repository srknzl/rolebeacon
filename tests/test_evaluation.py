from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

import pytest

from rolebeacon.config import Settings
from rolebeacon.domain import EligibilityStatus, ScoreResult
from rolebeacon.evaluation import evaluation_cases, evaluation_profile, run_model_evaluation, run_rules_evaluation
from rolebeacon.llm import LlmClient
from rolebeacon.scoring import evaluate_eligibility


class FixtureScorer:
    async def score(self, job: dict[str, Any], *_args: Any) -> ScoreResult:
        company = str(job["company"])
        title = str(job["title"])
        if company == "Global Data Co":
            return _score(88, [23, 18, 18, 10, 15, 4], evidence="Java, Go, Kafka, and distributed systems")
        if title == "Senior Frontend Engineer":
            return _score(35, [5, 0, 3, 8, 15, 4], gap="React and frontend experience")
        if company == "Google":
            return _score(68, [18, 8, 17, 8, 12, 5], evidence="distributed systems", gap="C++")
        return _score(70, [23, 18, 17, 9, 3, 0], evidence="Java and Kafka")


class RejectingScorer:
    async def score(self, job: dict[str, Any], *_args: Any) -> ScoreResult:
        raise ValueError(f"Rejected {job['title']}")


def _score(total: int, values: list[int], *, evidence: str = "", gap: str = "") -> ScoreResult:
    dimensions = dict(
        zip(
            ("role_domain", "stack", "domain_experience", "seniority", "location_authorization", "salary_employment"),
            values,
            strict=True,
        )
    )
    return ScoreResult(
        total=total,
        dimensions=dimensions,
        confidence=0.8,
        verdict="review",
        evidence=[{"requirement": "Relevant experience", "profile_evidence": evidence}] if evidence else [],
        gaps=[{"requirement": gap, "severity": "high"}] if gap else [],
        provider="fixture",
        model="fixture",
    )


def test_evaluation_dataset_exercises_eligibility_before_model_scoring() -> None:
    _, mobility, preferences, strategies = evaluation_profile()
    actual = {
        case.id: evaluate_eligibility(case.job, preferences, mobility, strategies).status
        for case in evaluation_cases()
    }

    assert actual == {case.id: case.expected_eligibility for case in evaluation_cases()}
    assert actual["no_sponsorship_blocker"] == EligibilityStatus.INELIGIBLE
    assert actual["remote_emea_scope_unknown"] == EligibilityStatus.UNKNOWN


async def test_model_evaluation_checks_scores_evidence_gaps_ranking_and_model_skip() -> None:
    report = await run_model_evaluation(FixtureScorer())

    assert report["passed"] is True
    assert report["summary"] == {
        "cases_passed": 5,
        "cases_total": 5,
        "model_calls": 4,
        "median_latency_seconds": 0.0,
        "max_latency_seconds": 0.0,
    }
    assert all(report["ranking_checks"].values())
    blocker = next(item for item in report["cases"] if item["id"] == "no_sponsorship_blocker")
    assert blocker["model_skipped"] is True


async def test_model_evaluation_reports_rejected_responses_instead_of_aborting() -> None:
    report = await run_model_evaluation(RejectingScorer())

    assert report["passed"] is False
    assert report["summary"]["model_calls"] == 4
    first_run = report["cases"][0]["runs"][0]
    assert first_run["checks"] == {"response": False}
    assert first_run["error"].startswith("ValueError: Rejected")


def test_rules_evaluation_checks_quality_invariants_and_repeatability() -> None:
    first = run_rules_evaluation(runs=4)
    second = run_rules_evaluation(runs=4)

    assert first == second
    assert first["passed"] is True
    assert first["summary"] == {"cases_passed": 5, "cases_total": 5, "runs_per_case": 4}
    assert all(first["ranking_checks"].values())
    assert all(case["checks"]["repeatable"] for case in first["cases"])
    assert all(case["checks"]["dimensions_sum"] for case in first["cases"])
    blocker = next(case for case in first["cases"] if case["id"] == "no_sponsorship_blocker")
    assert blocker["eligibility"] == "ineligible"
    assert blocker["verdict"] == "reject"
    assert blocker["score"] <= 39


@pytest.mark.skipif(os.getenv("ROLEBEACON_RUN_MODEL_EVAL") != "1", reason="requires an explicitly enabled model")
async def test_live_model_passes_rolebeacon_scoring_evaluation(tmp_path) -> None:
    settings = replace(
        Settings.load(tmp_path),
        llm_mode=os.getenv("ROLEBEACON_EVAL_PROVIDER", "ollama"),
        llm_enabled=True,
        llm_base_url=os.getenv("ROLEBEACON_EVAL_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/"),
        llm_model=os.getenv("ROLEBEACON_EVAL_MODEL", "qwen3:14b"),
        llm_api_key=os.getenv("ROLEBEACON_EVAL_API_KEY", ""),
    )
    report = await run_model_evaluation(LlmClient(settings))

    assert report["passed"] is True, report
