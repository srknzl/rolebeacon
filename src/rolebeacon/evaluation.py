from __future__ import annotations

import hashlib
import json
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .domain import EligibilityStatus, ScoreResult
from .profile import CandidateProfileV1, MobilityProfileV1, SearchPreferencesV1, generate_strategies
from .scoring import evaluate_eligibility, rule_score


class ModelScorer(Protocol):
    async def score(
        self,
        job: dict[str, Any],
        eligibility: Any,
        search_profile: dict[str, Any],
        candidate_profile: dict[str, Any],
    ) -> ScoreResult: ...


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    job: dict[str, Any]
    expected_eligibility: EligibilityStatus
    score_range: tuple[int, int] | None
    required_evidence_terms: tuple[str, ...] = ()
    required_gap_terms: tuple[str, ...] = ()
    max_location_score: int | None = None
    rules_score_range: tuple[int, int] = (0, 100)


def evaluation_profile() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    candidate = CandidateProfileV1.model_validate(
        {
            "schema_version": "1.0",
            "name": "Evaluation Candidate",
            "headline": "Senior backend and distributed-systems engineer",
            "summary": "Eight years of backend engineering with Java, Go, Kafka, and distributed systems.",
            "location": {"country_code": "TR", "country_name": "Türkiye", "city": "Istanbul"},
            "skills": {
                "Languages": ["Java", "Go", "Python"],
                "Backend": ["Distributed systems", "Kafka", "PostgreSQL", "Kubernetes"],
            },
            "experience": [
                {
                    "company": "Data Platform Co",
                    "title": "Senior Software Engineer",
                    "start": "2018",
                    "end": "2024",
                    "highlights": [
                        "Built Java and Go services for a distributed data platform.",
                        "Operated Kafka and Kubernetes workloads in production.",
                    ],
                }
            ],
        }
    )
    mobility = MobilityProfileV1.model_validate(
        {
            "schema_version": "1.0",
            "current_country_code": "TR",
            "work_authorizations": ["TR"],
            "relocation_targets": [{"country_code": "EUROPE", "country_name": "Europe"}],
            "remote_from_current_country": True,
            "willing_to_relocate": True,
            "contractor_allowed": True,
            "eor_allowed": True,
        }
    )
    preferences = SearchPreferencesV1.model_validate(
        {
            "schema_version": "1.0",
            "target_roles": ["Senior Backend Engineer", "Distributed Systems Engineer"],
            "preferred_skills": ["Java", "Go", "Kafka", "PostgreSQL", "Kubernetes"],
            "preferred_domains": ["distributed systems", "data infrastructure", "developer tools"],
            "preferred_seniority": ["senior", "staff"],
            "priority_companies": ["Google", "Microsoft"],
        }
    )
    strategies = [item.model_dump(mode="json") for item in generate_strategies(candidate, mobility, preferences)]
    return (
        candidate.model_dump(mode="json"),
        mobility.model_dump(mode="json"),
        preferences.model_dump(mode="json"),
        strategies,
    )


def evaluation_cases() -> tuple[EvaluationCase, ...]:
    return (
        EvaluationCase(
            id="strong_backend_match",
            job={
                "title": "Senior Backend Engineer",
                "company": "Global Data Co",
                "location": "Remote Worldwide",
                "remote_scope": "Worldwide",
                "employment_type": "full-time",
                "description": (
                    "Design distributed backend services using Java or Go. Operate Kafka, PostgreSQL, and "
                    "Kubernetes in production. Requires 6+ years of backend experience. Work remotely worldwide."
                ),
            },
            expected_eligibility=EligibilityStatus.ELIGIBLE,
            score_range=(72, 100),
            required_evidence_terms=("Java", "Go", "Kafka", "distributed"),
            rules_score_range=(65, 85),
        ),
        EvaluationCase(
            id="frontend_stack_mismatch",
            job={
                "title": "Senior Frontend Engineer",
                "company": "Web Studio",
                "location": "Remote Worldwide",
                "remote_scope": "Worldwide",
                "employment_type": "full-time",
                "description": (
                    "Build design systems and browser interfaces. Requires 5+ years of React, TypeScript, CSS, "
                    "accessibility, and frontend performance experience. Work remotely worldwide."
                ),
            },
            expected_eligibility=EligibilityStatus.ELIGIBLE,
            score_range=(0, 58),
            required_gap_terms=("React",),
            rules_score_range=(30, 55),
        ),
        EvaluationCase(
            id="big_tech_transferable_experience",
            job={
                "title": "Software Engineer, Storage Infrastructure",
                "company": "Google",
                "location": "Warsaw, Poland",
                "remote_scope": "",
                "employment_type": "full-time",
                "description": (
                    "Build large-scale distributed storage in C++. Experience with distributed systems and data "
                    "infrastructure is required. Relocation support and visa sponsorship are available."
                ),
            },
            expected_eligibility=EligibilityStatus.ELIGIBLE,
            score_range=(50, 85),
            required_evidence_terms=("distributed",),
            required_gap_terms=("C++",),
            rules_score_range=(50, 70),
        ),
        EvaluationCase(
            id="remote_emea_scope_unknown",
            job={
                "title": "Senior Backend Engineer",
                "company": "Regional Remote Co",
                "location": "Remote EMEA",
                "remote_scope": "EMEA",
                "employment_type": "full-time",
                "description": "Build Java and Kafka backend services. Candidates must be based in an eligible EMEA country.",
            },
            expected_eligibility=EligibilityStatus.UNKNOWN,
            score_range=(40, 82),
            required_evidence_terms=("Java", "Kafka"),
            max_location_score=8,
            rules_score_range=(45, 65),
        ),
        EvaluationCase(
            id="no_sponsorship_blocker",
            job={
                "title": "Senior Backend Engineer",
                "company": "Berlin Systems",
                "location": "Berlin, Germany",
                "remote_scope": "",
                "employment_type": "full-time",
                "description": (
                    "Build Java distributed systems. Applicants must already be authorized to work in Germany. "
                    "No visa sponsorship is available."
                ),
            },
            expected_eligibility=EligibilityStatus.INELIGIBLE,
            score_range=None,
            rules_score_range=(0, 39),
        ),
    )


def _mentions_any(items: list[dict[str, str]], field: str, terms: tuple[str, ...]) -> bool:
    if not terms:
        return True
    text = " ".join(str(item.get(field, "")) for item in items).casefold()
    return any(term.casefold() in text for term in terms)


def _positive_evidence(items: list[dict[str, str]]) -> bool:
    text = " ".join(str(item.get("profile_evidence", "")) for item in items).casefold()
    return not any(term in text for term in ("absent", "missing", "none", "no experience", "has no "))


async def run_model_evaluation(scorer: ModelScorer, runs: int = 1) -> dict[str, Any]:
    candidate, mobility, preferences, strategies = evaluation_profile()
    case_results: list[dict[str, Any]] = []
    scores_by_case: dict[str, list[int]] = {}
    latencies: list[float] = []

    for case in evaluation_cases():
        eligibility = evaluate_eligibility(case.job, preferences, mobility, strategies)
        checks = {"eligibility": eligibility.status == case.expected_eligibility}
        result: dict[str, Any] = {
            "id": case.id,
            "eligibility": eligibility.status.value,
            "route": eligibility.route,
            "checks": checks,
            "runs": [],
        }
        if eligibility.status == EligibilityStatus.INELIGIBLE:
            result["model_skipped"] = True
            result["passed"] = all(checks.values())
            case_results.append(result)
            continue

        scores_by_case[case.id] = []
        for _ in range(max(1, runs)):
            started = time.perf_counter()
            score = await scorer.score(case.job, eligibility, preferences, candidate)
            latency = time.perf_counter() - started
            latencies.append(latency)
            scores_by_case[case.id].append(score.total)
            run_checks = {
                "score_range": bool(case.score_range and case.score_range[0] <= score.total <= case.score_range[1]),
                "dimensions_sum": sum(score.dimensions.values()) == score.total,
                "evidence": _mentions_any(score.evidence, "profile_evidence", case.required_evidence_terms),
                "gaps": _mentions_any(score.gaps, "requirement", case.required_gap_terms),
                "positive_evidence": _positive_evidence(score.evidence),
                "confidence_range": 0 <= score.confidence <= 1,
                "location_constraint": (
                    case.max_location_score is None
                    or score.dimensions.get("location_authorization", 99) <= case.max_location_score
                ),
            }
            result["runs"].append(
                {
                    "score": score.total,
                    "dimensions": score.dimensions,
                    "verdict": score.verdict,
                    "confidence": score.confidence,
                    "evidence": score.evidence,
                    "gaps": score.gaps,
                    "latency_seconds": round(latency, 3),
                    "checks": run_checks,
                }
            )
            checks.update({f"run_{len(result['runs'])}_{key}": value for key, value in run_checks.items()})
        result["passed"] = all(checks.values())
        case_results.append(result)

    ranking_checks = {
        "strong_beats_frontend_by_15": (
            statistics.mean(scores_by_case.get("strong_backend_match", [0]))
            >= statistics.mean(scores_by_case.get("frontend_stack_mismatch", [0])) + 15
        ),
        "strong_beats_big_tech_transfer_by_5": (
            statistics.mean(scores_by_case.get("strong_backend_match", [0]))
            >= statistics.mean(scores_by_case.get("big_tech_transferable_experience", [0])) + 5
        ),
    }
    passed_cases = sum(bool(item["passed"]) for item in case_results)
    passed = passed_cases == len(case_results) and all(ranking_checks.values())
    return {
        "passed": passed,
        "summary": {
            "cases_passed": passed_cases,
            "cases_total": len(case_results),
            "model_calls": sum(len(item["runs"]) for item in case_results),
            "median_latency_seconds": round(statistics.median(latencies), 3) if latencies else 0,
            "max_latency_seconds": round(max(latencies), 3) if latencies else 0,
        },
        "ranking_checks": ranking_checks,
        "cases": case_results,
    }


def run_rules_evaluation(runs: int = 3) -> dict[str, Any]:
    candidate, mobility, preferences, strategies = evaluation_profile()
    case_results: list[dict[str, Any]] = []
    scores_by_case: dict[str, list[int]] = {}
    repeat_count = max(2, runs)

    for case in evaluation_cases():
        eligibility = evaluate_eligibility(case.job, preferences, mobility, strategies)
        serialized_runs: list[str] = []
        rendered_runs: list[dict[str, Any]] = []
        scores_by_case[case.id] = []
        for _ in range(repeat_count):
            score = rule_score(case.job, eligibility, preferences, candidate, strategies)
            score_value = asdict(score)
            serialized_runs.append(json.dumps(score_value, sort_keys=True, separators=(",", ":")))
            rendered_runs.append(score_value)
            scores_by_case[case.id].append(score.total)

        first = rendered_runs[0]
        checks = {
            "eligibility": eligibility.status == case.expected_eligibility,
            "score_range": case.rules_score_range[0] <= first["total"] <= case.rules_score_range[1],
            "dimensions_sum": sum(first["dimensions"].values()) == first["total"],
            "dimension_bounds": _valid_dimension_bounds(first["dimensions"]),
            "provider": first["provider"] == "rules" and first["model"] == "deterministic-v2",
            "repeatable": len(set(serialized_runs)) == 1,
            "location_constraint": (
                case.max_location_score is None
                or first["dimensions"].get("location_authorization", 99) <= case.max_location_score
            ),
            "blocker_verdict": (
                eligibility.status != EligibilityStatus.INELIGIBLE
                or (first["verdict"] == "reject" and first["total"] <= 39)
            ),
        }
        case_results.append(
            {
                "id": case.id,
                "eligibility": eligibility.status.value,
                "route": eligibility.route,
                "score": first["total"],
                "dimensions": first["dimensions"],
                "verdict": first["verdict"],
                "evidence": first["evidence"],
                "gaps": first["gaps"],
                "repeat_signature": hashlib.sha256(serialized_runs[0].encode()).hexdigest(),
                "checks": checks,
                "passed": all(checks.values()),
            }
        )

    ranking_checks = {
        "strong_beats_frontend_by_15": scores_by_case["strong_backend_match"][0]
        >= scores_by_case["frontend_stack_mismatch"][0] + 15,
        "strong_beats_big_tech_transfer_by_5": scores_by_case["strong_backend_match"][0]
        >= scores_by_case["big_tech_transferable_experience"][0] + 5,
    }
    passed_cases = sum(bool(item["passed"]) for item in case_results)
    return {
        "engine": "rules:deterministic-v2",
        "passed": passed_cases == len(case_results) and all(ranking_checks.values()),
        "summary": {
            "cases_passed": passed_cases,
            "cases_total": len(case_results),
            "runs_per_case": repeat_count,
        },
        "ranking_checks": ranking_checks,
        "cases": case_results,
    }


def _valid_dimension_bounds(dimensions: dict[str, int]) -> bool:
    maximums = {
        "role_domain": 25,
        "stack": 20,
        "domain_experience": 20,
        "seniority": 10,
        "location_authorization": 15,
        "salary_employment": 10,
    }
    return set(dimensions) == set(maximums) and all(0 <= dimensions[key] <= maximum for key, maximum in maximums.items())
