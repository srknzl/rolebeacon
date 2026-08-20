"""Regenerate the README screenshots from a synthetic installation.

    uv run python tools/screenshots.py

Everything the shots show is invented here: the candidate, the companies, the postings, and the
source health. The installation is seeded into a throwaway ROLEBEACON_DATA_DIR with auto-sync
off and served in-process, so no external source is contacted at any point and nothing touches
the real application-data directory.

Eligibility and scores are not typed in. The seed writes the postings and then runs the real
pipeline over them through `SyncService.run(collect=False)` - the rescore path - so the numbers
on the page are what `evaluate_eligibility` and `rule_score` actually produce for this profile
against these postings. Change a scoring rule and the next run of this tool shows it.

Framing matches the committed set: 1280x720, light palette, top of the page unless a shot is
about something further down. `source-health.png` is a clip of its own section.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import pathlib
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "docs" / "screenshots"
NOW = datetime.now(UTC)


def ago(hours: float) -> datetime:
    """How long ago a posting was published."""
    return NOW - timedelta(hours=hours)


def moments_ago(seconds: float) -> datetime:
    """A run duration, not a posting age: finish_sync_run measures from here to now."""
    return datetime.now(UTC) - timedelta(seconds=seconds)


SETUP_PAYLOAD = {
    "candidate": {
        "schema_version": "1.0",
        "name": "Alex Demir",
        "headline": "Senior Backend Engineer · distributed systems",
        "summary": (
            "Backend engineer with nine years on high-throughput distributed systems. Builds and "
            "operates event-driven services in Go and Java, owns them in production, and has led "
            "two platform migrations end to end."
        ),
        "contact": {"email": "alex.demir@example.com", "phone": ""},
        "location": {"country_code": "TR", "country_name": "Türkiye", "city": "Istanbul"},
        "skills": {
            "Languages": ["Go", "Java", "Python", "SQL"],
            "Platform": ["Kubernetes", "Kafka", "PostgreSQL", "Terraform", "gRPC"],
            "Practices": ["Distributed systems", "Observability", "Performance tuning"],
        },
        "experience": [
            {
                "company": "Meridian Payments",
                "title": "Senior Backend Engineer",
                "start": "2021-03", "end": "", "location": "Istanbul, Türkiye",
                "highlights": [
                    "Owns the ledger service handling 40k settlement events per minute.",
                    "Led the migration from a monolithic scheduler to Kafka-backed workers.",
                ],
            },
            {
                "company": "Northwind Data",
                "title": "Backend Engineer",
                "start": "2017-01", "end": "2021-02", "location": "Istanbul, Türkiye",
                "highlights": ["Built the multi-tenant ingestion API serving 200 customers."],
            },
        ],
        "projects": [
            {
                "name": "kafka-replay",
                "summary": "Command-line tool for replaying a Kafka topic into a test environment.",
                "highlights": [], "technologies": ["Go", "Kafka"],
            }
        ],
        "education": [
            {"institution": "Boğaziçi University", "degree": "BSc", "field": "Computer Engineering",
             "start": "2012", "end": "2016"}
        ],
        "languages": [
            {"name": "Turkish", "proficiency": "Native"},
            {"name": "English", "proficiency": "Professional"},
        ],
    },
    "mobility": {
        "schema_version": "1.0",
        "current_country_code": "TR",
        "work_authorizations": ["TR"],
        "relocation_targets": [
            {"country_code": "DE", "country_name": "Germany", "cities": ["Berlin", "Munich"]},
            {"country_code": "NL", "country_name": "Netherlands", "cities": ["Amsterdam"]},
            {"country_code": "IE", "country_name": "Ireland", "cities": ["Dublin"]},
        ],
        "remote_from_current_country": True,
        "sponsorship_required_outside_authorized_countries": True,
        "timezone": "Europe/Istanbul",
    },
    "preferences": {
        "schema_version": "1.0",
        "target_roles": ["Backend Engineer", "Platform Engineer", "Distributed Systems Engineer"],
        "preferred_skills": ["Go", "Java", "Kafka", "Kubernetes", "PostgreSQL", "gRPC"],
        "preferred_domains": ["Fintech", "Developer infrastructure"],
        "preferred_seniority": ["senior", "staff"],
        "priority_companies": ["Larkspur Systems", "Northgate Cloud"],
        "company_watchlist": ["Vantage Metrics"],
        "salary": {"currency": "EUR", "minimum": 85000},
    },
    # Filled in from SOURCES below: setup.json is the authority for enablement, not sources.json.
    "enabled_source_ids": [],
    "llm": {"mode": "rules", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"},
    "activate": True,
}

SOURCES = [
    {"id": "greenhouse-larkspur", "kind": "greenhouse", "name": "Larkspur Systems", "enabled": True,
     "company": "Larkspur Systems", "slug": "larkspur", "url": "https://boards.greenhouse.io/larkspur"},
    {"id": "lever-northgate", "kind": "lever", "name": "Northgate Cloud", "enabled": True,
     "company": "Northgate Cloud", "slug": "northgate", "url": "https://jobs.lever.co/northgate"},
    {"id": "ashby-vantage", "kind": "ashby", "name": "Vantage Metrics", "enabled": True,
     "company": "Vantage Metrics", "slug": "vantage", "url": "https://jobs.ashbyhq.com/vantage"},
    {"id": "smartrecruiters-orrery", "kind": "smartrecruiters", "name": "Orrery Group", "enabled": True,
     "company": "Orrery Group", "slug": "orrery", "url": "https://careers.smartrecruiters.com/Orrery"},
    {"id": "himalayas", "kind": "himalayas", "name": "Himalayas", "enabled": True,
     "url": "https://himalayas.app/jobs/api"},
    {"id": "arbeitnow", "kind": "arbeitnow", "name": "Arbeitnow", "enabled": True,
     "url": "https://www.arbeitnow.com/api/job-board-api"},
    {"id": "remotive", "kind": "remotive", "name": "Remotive", "enabled": True,
     "url": "https://remotive.com/api/remote-jobs"},
    {"id": "linkedin-europe", "kind": "linkedin", "name": "LinkedIn — Europe", "enabled": True,
     "url": "https://www.linkedin.com/jobs/search"},
    {"id": "workday-caldera", "kind": "workday", "name": "Caldera Robotics", "enabled": True,
     "company": "Caldera Robotics", "slug": "caldera", "url": "https://caldera.wd1.myworkdayjobs.com/careers"},
    {"id": "personio-brightloom", "kind": "personio", "name": "Brightloom GmbH", "enabled": False,
     "company": "Brightloom GmbH", "slug": "brightloom", "url": "https://brightloom.jobs.personio.de"},
]

# (source, id, title, company, location, remote_scope, salary, published hours ago, description)
JOBS = [
    ("greenhouse-larkspur", "lk-4021", "Senior Backend Engineer, Payments Platform", "Larkspur Systems",
     "Berlin, Germany", "hybrid", (95000, 125000, "EUR"), 6,
     """Larkspur Systems is hiring a Senior Backend Engineer for the Payments Platform team in Berlin.

You will own the settlement pipeline end to end: a Go and Java service estate processing several
million events a day on Kafka, backed by PostgreSQL and deployed to Kubernetes.

What you will do:
- Design and operate event-driven services with gRPC interfaces between them.
- Improve throughput and tail latency of the settlement path.
- Take part in the on-call rotation for the services your team owns.

What we look for:
- 5+ years building backend services in Go or Java.
- Production experience with Kafka and PostgreSQL at scale.
- Comfort operating on Kubernetes and reasoning about distributed failure.

We sponsor visas for this role and provide a relocation package for candidates moving to Germany."""),

    ("lever-northgate", "ng-7714", "Staff Platform Engineer", "Northgate Cloud",
     "Remote — Europe", "remote", (110000, 140000, "EUR"), 20,
     """Northgate Cloud runs developer infrastructure for teams that ship continuously. We are looking
for a Staff Platform Engineer to lead the internal compute platform.

The role:
- Own the Kubernetes-based build and deploy platform used by 300 engineers.
- Design the multi-tenant isolation model and its Terraform interfaces.
- Set the observability standard across the estate.

Requirements:
- 8+ years in backend or platform engineering, including Go.
- Deep Kubernetes and Terraform experience.
- Track record leading a platform migration.

This role is remote within Europe. We can sponsor a work visa where required, and offer a
relocation budget for candidates who prefer to move to one of our hubs."""),

    ("ashby-vantage", "vm-1180", "Backend Engineer, Data Ingestion", "Vantage Metrics",
     "Amsterdam, Netherlands", "hybrid", (80000, 105000, "EUR"), 30,
     """Vantage Metrics builds observability tooling. Our ingestion tier accepts several hundred
thousand metrics per second and we need a backend engineer to grow it.

You will work in Go on the ingestion API, the Kafka buffering layer, and the storage adapters
that write into our columnar store.

Requirements:
- 4+ years of backend engineering, ideally in Go.
- Experience with high-throughput data pipelines.
- Working knowledge of PostgreSQL and Kubernetes.

Visa sponsorship is available for this position. Relocation assistance is offered."""),

    ("himalayas", "hm-9903", "Distributed Systems Engineer", "Orrery Group",
     "Remote Worldwide", "remote", (120000, 160000, "USD"), 3,
     """Orrery Group is a fully distributed company building consensus infrastructure.

We are hiring a Distributed Systems Engineer to work on the replication layer: Raft-based
coordination, gRPC transport, and the storage engine underneath.

You should have:
- Strong systems fundamentals and experience in Go, Java, or Rust.
- Practical understanding of consensus, replication, and partial failure.
- 6+ years of production backend experience.

This is a remote worldwide role. We hire through an employer of record in most countries."""),

    ("greenhouse-larkspur", "lk-4088", "Senior Software Engineer, Ledger", "Larkspur Systems",
     "Munich, Germany", "onsite", (98000, 122000, "EUR"), 48,
     """Join the Ledger team at Larkspur Systems in Munich.

The ledger is the source of truth for every transaction we process. You will work in Java on its
core write path, its reconciliation jobs, and the gRPC API other teams build on.

What we need:
- 5+ years with Java in production.
- Experience with PostgreSQL, correctness under concurrency, and financial data.
- Familiarity with Kafka.

This position is based in our Munich office. We offer visa sponsorship and relocation support."""),

    ("arbeitnow", "ab-5521", "Backend Engineer (Go)", "Brightloom GmbH",
     "Berlin, Germany", "hybrid", (75000, 95000, "EUR"), 54,
     """Brightloom is a Berlin-based logistics startup. We are looking for a backend engineer to join
the routing team.

Our stack is Go, PostgreSQL, and Kubernetes on GCP. You will build the services that plan and
re-plan several thousand routes an hour.

Requirements:
- 3+ years of Go in production.
- Solid SQL and an interest in optimisation problems.

Please note: we are unable to provide visa sponsorship for this role. Applicants must already hold
the right to work in Germany. No relocation assistance is available."""),

    ("remotive", "rm-3312", "Senior Backend Engineer", "Copperline Health",
     "Remote (US only)", "remote", (150000, 185000, "USD"), 26,
     """Copperline Health is hiring a Senior Backend Engineer for our clinical data platform.

You will work in Python and Go on the pipeline that normalises records from hospital systems.

Requirements:
- 5+ years of backend experience.
- Familiarity with HL7/FHIR is a plus, not a requirement.

This role is open to applicants based in the United States only. We are not able to sponsor visas
and cannot support relocation at this time."""),

    ("linkedin-europe", "li-88120", "Platform Engineer, Developer Experience", "Kestrel Labs",
     "Dublin, Ireland", "hybrid", (90000, 115000, "EUR"), 12,
     """Kestrel Labs is growing its Developer Experience group in Dublin.

You will own the CI platform: build orchestration, artifact caching, and the Terraform modules
teams use to describe their own infrastructure.

Requirements:
- Backend or platform engineering experience with Go or Python.
- Kubernetes in production.
- An eye for the parts of a build system engineers actually complain about.

We provide visa sponsorship and a relocation package for candidates moving to Ireland."""),

    ("linkedin-europe", "li-88455", "Senior Backend Engineer, Search", "Vantage Metrics",
     "Remote — Netherlands", "remote", (85000, 110000, "EUR"), 40,
     """Vantage Metrics is hiring a Senior Backend Engineer for the Search team.

You will work in Java on the query planner and the distributed index behind it.

Requirements:
- 5+ years of backend engineering.
- Experience with search or database internals.
- Comfort with Kubernetes.

This role is remote within the Netherlands; you must be able to work from the Netherlands."""),

    ("smartrecruiters-orrery", "or-2201", "Engineering Manager, Platform", "Orrery Group",
     "Remote — Europe", "remote", (130000, 165000, "EUR"), 70,
     """Orrery Group is looking for an Engineering Manager to lead the Platform group.

You will manage a team of eight engineers, own the roadmap for the internal compute platform, and
partner with product on capacity planning.

Requirements:
- 3+ years managing engineers.
- A background in backend or infrastructure engineering.

Remote within Europe. Sponsorship available."""),

    ("himalayas", "hm-9975", "Backend Engineer, Billing", "Northgate Cloud",
     "Remote — Europe", "remote", (85000, 105000, "EUR"), 90,
     """Northgate Cloud is hiring a Backend Engineer for the Billing team.

You will work in Go on metering, invoicing, and the reconciliation jobs between them. The service
runs on Kubernetes and stores in PostgreSQL.

Requirements:
- 4+ years of backend engineering.
- Go and PostgreSQL.
- Care about getting numbers right.

Remote within Europe. Visa sponsorship available for candidates relocating to a hub."""),

    ("ashby-vantage", "vm-1204", "Product Manager, Observability", "Vantage Metrics",
     "Amsterdam, Netherlands", "hybrid", (85000, 105000, "EUR"), 33,
     """Vantage Metrics is hiring a Product Manager for the Observability product line.

You will own the roadmap for dashboards and alerting, run discovery with customers, and write the
specifications engineering builds from.

Requirements:
- 4+ years of product management in a technical B2B product.
- Comfort talking to engineers about traces and metrics.

Visa sponsorship available."""),

    ("remotive", "rm-3390", "Sales Engineer, EMEA", "Kestrel Labs",
     "Remote — EMEA", "remote", (70000, 95000, "EUR"), 61,
     """Kestrel Labs is hiring a Sales Engineer for EMEA.

You will run technical evaluations with prospective customers, build proofs of concept, and work
with the account team through procurement.

Requirements:
- Experience in a pre-sales or solutions role at a developer-tools company.
- Enough engineering background to build a convincing demo.

Remote within EMEA."""),

    ("greenhouse-larkspur", "lk-4102", "Senior Backend Engineer, Risk", "Larkspur Systems",
     "Berlin, Germany", "hybrid", (95000, 120000, "EUR"), 100,
     """Larkspur Systems is hiring a Senior Backend Engineer for the Risk team in Berlin.

You will build the real-time scoring service that decides whether a transaction proceeds. Go and
Java, Kafka in front, PostgreSQL behind.

Requirements:
- 5+ years of backend engineering.
- Experience with low-latency services.
- Kafka and Kubernetes.

Visa sponsorship and relocation support are provided."""),

    ("lever-northgate", "ng-7802", "Staff Backend Engineer, API", "Northgate Cloud",
     "Remote — Europe", "remote", (115000, 145000, "EUR"), 130,
     """Northgate Cloud is hiring a Staff Backend Engineer for the public API.

You will own the gRPC and REST surface that customers integrate against: versioning, rate limits,
and the compatibility guarantees behind them.

Requirements:
- 8+ years of backend engineering, including Go.
- Experience designing APIs other people depend on.
- Kubernetes and PostgreSQL.

Remote within Europe. Sponsorship available."""),
    ("greenhouse-larkspur", "lk-4130", "Senior Backend Engineer, Settlement", "Larkspur Systems",
     "Berlin, Germany", "hybrid", (100000, 128000, "EUR"), 9,
     """Larkspur Systems is hiring a Senior Backend Engineer for the Settlement team in Berlin.

Settlement is the last mile of the payments path: batching, netting, and the reconciliation jobs
that prove the books balance. The stack is Go and Java on Kafka, with PostgreSQL underneath and
gRPC between services on Kubernetes.

Requirements:
- 5+ years of backend engineering in Go or Java.
- Kafka and PostgreSQL in production.
- Experience with systems where correctness matters more than throughput.

Visa sponsorship and a relocation package are provided for candidates moving to Germany."""),

    ("lever-northgate", "ng-7850", "Senior Platform Engineer, Compute", "Northgate Cloud",
     "Remote — Europe", "remote", (100000, 130000, "EUR"), 16,
     """Northgate Cloud is hiring a Senior Platform Engineer for the Compute team.

You will work on the scheduler that places customer workloads across our Kubernetes fleet: bin
packing, preemption, and the Terraform interfaces teams use to ask for capacity.

Requirements:
- 5+ years of backend or platform engineering, including Go.
- Kubernetes internals beyond kubectl.
- Terraform, and an opinion about it.

Remote within Europe. We sponsor work visas and offer a relocation budget for our Berlin and
Dublin hubs."""),

    ("himalayas", "hm-10021", "Backend Engineer, Storage", "Orrery Group",
     "Remote Worldwide", "remote", (110000, 145000, "USD"), 22,
     """Orrery Group is hiring a Backend Engineer for the Storage team.

You will work on the log-structured storage engine behind our coordination service: compaction,
crash recovery, and the gRPC read path in front of it.

Requirements:
- 4+ years of systems or backend engineering in Go, Java, or Rust.
- Comfort reasoning about durability and partial failure.
- Interest in performance work backed by measurement.

Remote worldwide. We hire through an employer of record where we have no entity."""),

    ("ashby-vantage", "vm-1250", "Senior Backend Engineer, Query", "Vantage Metrics",
     "Amsterdam, Netherlands", "hybrid", (90000, 115000, "EUR"), 36,
     """Vantage Metrics is hiring a Senior Backend Engineer for the Query team.

You will work in Java and Go on the query planner, the distributed execution layer, and the cache
in front of both.

Requirements:
- 5+ years of backend engineering.
- Experience with query execution, database internals, or a comparable systems problem.
- Kubernetes and PostgreSQL.

Visa sponsorship is available for this position, and relocation assistance is offered."""),

    ("linkedin-europe", "li-88790", "Staff Software Engineer, Infrastructure", "Kestrel Labs",
     "Dublin, Ireland", "hybrid", (110000, 140000, "EUR"), 44,
     """Kestrel Labs is hiring a Staff Software Engineer for Infrastructure in Dublin.

You will set the technical direction for the service platform: the Go service framework, the
gRPC conventions on top of it, and the Kubernetes primitives underneath.

Requirements:
- 8+ years of backend engineering.
- Deep Go and Kubernetes experience.
- A record of technical leadership without a management title.

We provide visa sponsorship and relocation support for candidates moving to Ireland."""),

    ("arbeitnow", "ab-5610", "Backend Engineer, Platform", "Caldera Robotics",
     "Munich, Germany", "onsite", (80000, 100000, "EUR"), 58,
     """Caldera Robotics is hiring a Backend Engineer for the Platform team in Munich.

You will build the services that collect telemetry from our fleet and the APIs the operations
console reads. Go, PostgreSQL, and Kubernetes.

Requirements:
- 3+ years of backend engineering.
- Go or a strong reason you will pick it up quickly.
- Interest in hardware-adjacent systems.

This position is based in Munich. Visa sponsorship is available and we support relocation."""),

    ("remotive", "rm-3450", "Senior Backend Engineer, Integrations", "Copperline Health",
     "Remote — EMEA", "remote", (90000, 115000, "EUR"), 78,
     """Copperline Health is hiring a Senior Backend Engineer for the Integrations team.

You will build and operate the connectors that pull records out of hospital systems, normalise
them, and land them in our platform. Python and Go, Kafka in the middle.

Requirements:
- 5+ years of backend engineering.
- Experience with messy third-party integrations.
- Kafka and PostgreSQL.

This role is remote within EMEA."""),

    ("smartrecruiters-orrery", "or-2260", "Distributed Systems Engineer, Coordination", "Orrery Group",
     "Remote Worldwide", "remote", (125000, 165000, "USD"), 110,
     """Orrery Group is hiring a Distributed Systems Engineer for the Coordination team.

You will work on the consensus layer itself: leader election, membership changes, and the tests
that prove the invariants hold under partition.

Requirements:
- 6+ years of systems engineering.
- Working knowledge of Raft or Paxos beyond the papers.
- Go, Java, or Rust in production.

Remote worldwide, hired through an employer of record where needed."""),
]

COMPANIES = [
    {
        "name": "Northgate Cloud", "domain": "northgate.example",
        "profile": {
            "summary": "Remote-first within Europe, with hubs in Berlin and Dublin. Visa sponsorship is "
                       "stated on the careers site. Engineering publishes architecture write-ups regularly.",
            "industry": "Developer infrastructure", "remote_policy": "regional",
            "sponsorship": "available", "relocation": "available",
            "engineering_signals": [
                "Public engineering blog with post-incident write-ups.",
                "Open-source Terraform provider maintained by the platform team.",
                "Documented on-call rotation and compensation policy.",
            ],
            "risks": ["Careers page does not state a compensation range for European roles."],
            "confidence": 0.0,
        },
        "evidence": [
            {"source_url": "https://northgate.example/careers", "source_type": "careers",
             "title": "Careers — Northgate Cloud",
             "excerpt": "We sponsor work visas for engineering roles and provide a relocation budget for candidates moving to Berlin or Dublin."},
            {"source_url": "https://northgate.example/engineering", "source_type": "engineering",
             "title": "Engineering — Northgate Cloud",
             "excerpt": "Every service is owned by the team that built it, including its on-call rotation."},
            {"source_url": "https://northgate.example/about", "source_type": "about",
             "title": "About — Northgate Cloud",
             "excerpt": "Northgate Cloud is remote-first across Europe with offices in Berlin and Dublin."},
            {"source_url": "https://northgate.example/handbook/remote", "source_type": "handbook",
             "title": "Working remotely — Northgate Cloud",
             "excerpt": "Employees may work from any country in which we have an entity or an employer-of-record arrangement."},
        ],
        "score": {"total": 78, "dimensions": {
            "domain_alignment": 16, "engineering_environment": 17, "location_mobility": 18,
            "compensation": 9, "company_quality": 12, "evidence_confidence": 6},
            "reasons": [
                "Official careers page states visa sponsorship and a relocation budget.",
                "Engineering pages describe team-owned services and a documented on-call rotation.",
                "Remote policy is stated as regional across Europe on two official pages.",
            ],
            "risks": ["No compensation range is published for European roles."]},
    },
    {
        "name": "Larkspur Systems", "domain": "larkspur.example",
        "profile": {
            "summary": "Payments company with offices in Berlin and Munich. Sponsorship and relocation are "
                       "stated for engineering roles. Remote work is described as hybrid, not remote-first.",
            "industry": "Fintech", "remote_policy": "hybrid",
            "sponsorship": "available", "relocation": "available",
            "engineering_signals": [
                "Publishes a yearly reliability report with measured availability.",
                "Careers page names the languages and datastores each team uses.",
            ],
            "risks": [
                "Hybrid policy requires three days a week in a German office.",
                "No public engineering blog beyond the reliability report.",
            ],
            "confidence": 0.0,
        },
        "evidence": [
            {"source_url": "https://larkspur.example/careers", "source_type": "careers",
             "title": "Careers — Larkspur Systems",
             "excerpt": "Engineering roles in Germany include visa sponsorship and a relocation package."},
            {"source_url": "https://larkspur.example/careers/engineering", "source_type": "careers",
             "title": "Engineering at Larkspur",
             "excerpt": "Teams work in Go and Java on Kafka and PostgreSQL, deployed to Kubernetes."},
            {"source_url": "https://larkspur.example/reliability-2026", "source_type": "engineering",
             "title": "Reliability report 2026",
             "excerpt": "The settlement path met its 99.98% availability objective across the year."},
        ],
        "score": {"total": 71, "dimensions": {
            "domain_alignment": 18, "engineering_environment": 14, "location_mobility": 14,
            "compensation": 10, "company_quality": 10, "evidence_confidence": 5},
            "reasons": [
                "Careers page states visa sponsorship and relocation for German engineering roles.",
                "Domain matches the candidate's stated fintech preference.",
                "Published reliability report gives a measured availability figure.",
            ],
            "risks": ["Hybrid policy requires three office days a week in Germany."]},
    },
    {
        "name": "Vantage Metrics", "domain": "vantage.example",
        "profile": {
            "summary": "Observability vendor headquartered in Amsterdam. Sponsorship is stated on the careers "
                       "page. Remote work is described as within the Netherlands, not worldwide.",
            "industry": "Developer infrastructure", "remote_policy": "regional",
            "sponsorship": "available", "relocation": "unknown",
            "engineering_signals": ["Careers page lists the on-call expectation for each team."],
            "risks": ["Relocation support is not mentioned on any fetched official page."],
            "confidence": 0.0,
        },
        "evidence": [
            {"source_url": "https://vantage.example/careers", "source_type": "careers",
             "title": "Careers — Vantage Metrics",
             "excerpt": "We sponsor visas for roles based in the Netherlands. Remote roles are open to candidates who can work from the Netherlands."},
            {"source_url": "https://vantage.example/about", "source_type": "about",
             "title": "About — Vantage Metrics",
             "excerpt": "Vantage Metrics is headquartered in Amsterdam."},
        ],
        "score": {"total": 62, "dimensions": {
            "domain_alignment": 15, "engineering_environment": 11, "location_mobility": 13,
            "compensation": 8, "company_quality": 11, "evidence_confidence": 4},
            "reasons": [
                "Careers page states visa sponsorship for Netherlands-based roles.",
                "Remote wording is country-scoped, which matches the posting's own scope.",
            ],
            "risks": ["Relocation support is not stated on any official page that was fetched."]},
    },
]


def seed(data_dir: pathlib.Path) -> Any:
    """Write the invented installation, then score it with the real pipeline."""
    import asyncio

    from rolebeacon.company import RULES_MODEL
    from rolebeacon.config import Settings
    from rolebeacon.database import Database
    from rolebeacon.domain import CollectedJob, JobStatus
    from rolebeacon.llm import LlmClient
    from rolebeacon.setup import SetupService
    from rolebeacon.sync import SyncService

    data_dir.mkdir(parents=True, exist_ok=True)
    # Written before setup completes: the ids have to exist before they can be enabled, and
    # _merge_default_sources then adds the shipped catalog around them.
    (data_dir / "sources.json").write_text(json.dumps(SOURCES, indent=2) + "\n")
    (data_dir / "sources.json").chmod(0o600)
    payload = {**SETUP_PAYLOAD, "enabled_source_ids": [s["id"] for s in SOURCES if s["enabled"]]}
    SetupService(Settings.load()).complete(payload)

    settings = Settings.load()
    database = Database(settings.database_path)
    database.initialize()

    ids: dict[str, int] = {}
    for source, source_job_id, title, company, location, scope, salary, hours, description in JOBS:
        job_id, _ = database.upsert_job(CollectedJob(
            source=source, source_job_id=source_job_id, title=title, company=company,
            location=location, description=description,
            url=f"https://{source.split('-')[0]}.example/jobs/{source_job_id}",
            remote_scope=scope, employment_type="full_time",
            salary_min=salary[0], salary_max=salary[1], salary_currency=salary[2],
            published_at=ago(hours), updated_at=ago(hours),
        ))
        ids[source_job_id] = job_id

    # The real pipeline, over the postings above, contacting nothing.
    sync = SyncService(settings, database, LlmClient(settings))
    asyncio.run(sync.run(force=True, manual=True, collect=False))

    # A pipeline a person has actually been working. Decided jobs are hidden from the Jobs list
    # by default, so the corpus is sized to leave a healthy undecided queue behind them.
    for source_job_id, status in [
        ("lk-4021", JobStatus.BOOKMARKED), ("ng-7714", JobStatus.BOOKMARKED),
        ("hm-9903", JobStatus.BOOKMARKED), ("li-88120", JobStatus.APPLIED),
        ("lk-4088", JobStatus.APPLIED), ("vm-1180", JobStatus.OFFER),
        ("ng-7802", JobStatus.REJECTED), ("ab-5521", JobStatus.NOT_INTERESTED),
    ]:
        database.save_feedback(ids[source_job_id], status)
    for source_job_id in ("li-88120", "lk-4088", "vm-1180"):
        database.save_application(
            ids[source_job_id], status="prepared",
            resume_path=str(data_dir / "applications" / str(ids[source_job_id]) / "resume.pdf"),
        )

    for company in COMPANIES:
        database.save_company_research(
            name=company["name"], domain=company["domain"], profile=company["profile"],
            evidence=company["evidence"], score=company["score"], provider="rules", model=RULES_MODEL,
        )

    # Source health: mostly healthy, one failing, one holding back an anomalous snapshot. These
    # are the states the Sources page exists to tell apart, so the shot has to show all three.
    healthy = {
        "greenhouse-larkspur": (5, 5), "lever-northgate": (3, 3), "ashby-vantage": (3, 3),
        "himalayas": (3, 3), "arbeitnow": (2, 2), "remotive": (3, 3), "linkedin-europe": (3, 3),
    }
    for source_id, (seen, changed) in healthy.items():
        run_id = database.start_sync_run(source_id)
        database.finish_source(source_id, seen=seen, changed=changed)
        database.finish_sync_run(
            run_id, status="ok", started_at=moments_ago(1.4), jobs_seen=seen, jobs_new=changed,
            jobs_changed=changed, requests_made=2,
        )

    run_id = database.start_sync_run("smartrecruiters-orrery")
    database.finish_source(
        "smartrecruiters-orrery", seen=2, changed=1, truncated=True,
        snapshot_warning="Provider returned 2 jobs against a baseline of 46. Active jobs are preserved "
                         "until the same complete set of source job IDs is observed again.",
    )
    database.finish_sync_run(
        run_id, status="ok", started_at=moments_ago(0.9), jobs_seen=2, jobs_new=1, jobs_changed=1,
        requests_made=1, truncated=True,
    )

    run_id = database.start_sync_run("workday-caldera")
    database.fail_source("workday-caldera", "HTTP 403 from the provider endpoint after 3 attempts.")
    database.finish_sync_run(
        run_id, status="error", started_at=moments_ago(2.6), requests_made=3,
        error="HTTP 403 from the provider endpoint after 3 attempts.",
    )
    print(f"seeded {len(ids)} jobs and {len(COMPANIES)} companies in {data_dir}")
    return settings


@contextlib.contextmanager
def serving(settings: Any, port: int) -> Iterator[str]:
    """Serve the seeded installation on a background thread for the browser to read."""
    import uvicorn

    from rolebeacon.app import create_app

    # The local-origin guard compares the request against settings.port, so serving on any other
    # port would 403 every page. Tell the app which port it is actually on.
    app = create_app(dataclasses.replace(settings, port=port))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base}/api/sync/status", timeout=1).read()
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    else:
        raise RuntimeError(f"the screenshot server did not answer on {base}")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def shoot(base: str, out: pathlib.Path) -> None:
    from playwright.sync_api import Page, sync_playwright

    def settle(page: Page) -> None:
        """Facet counts, company suggestions, and sync status all arrive after load."""
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(400)

    def capture(page: Page, name: str) -> None:
        page.screenshot(path=out / f"{name}.png")
        print(f"  {name}.png")

    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as play:
        browser = play.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        # The palette is a stored browser choice now, so the shots pin it rather than inheriting
        # whichever theme the machine taking them happens to be in.
        context.add_init_script("localStorage.setItem('rolebeacon.theme', 'light')")
        page = context.new_page()

        page.goto(f"{base}/", wait_until="load")
        settle(page)
        capture(page, "dashboard")

        page.goto(f"{base}/jobs", wait_until="load")
        settle(page)
        capture(page, "jobs")

        # The job the list leads with, so the detail shots follow on from it.
        href = page.get_attribute(".job-list .job-card a[href^='/jobs/']", "href")
        page.goto(f"{base}{href}", wait_until="load")
        settle(page)
        capture(page, "job-detail")

        # Score breakdown with its weakest factor open: the shot is about what a factor says,
        # not that a list of them exists.
        page.evaluate("""() => {
            const factor = document.querySelector('details.score-factor');
            if (factor) factor.open = true;
            const heading = [...document.querySelectorAll('h3')].find(h => h.textContent.includes('Score breakdown'));
            if (heading) window.scrollTo(0, heading.getBoundingClientRect().top + window.scrollY - 96);
        }""")
        page.wait_for_timeout(400)
        capture(page, "score-factors")

        page.goto(f"{base}/companies", wait_until="load")
        settle(page)
        company = page.get_attribute("a[href^='/companies/']", "href")
        page.goto(f"{base}{company}", wait_until="load")
        settle(page)
        capture(page, "company-detail")

        page.goto(f"{base}/applications", wait_until="load")
        settle(page)
        capture(page, "job-tracking")

        page.goto(f"{base}/sources", wait_until="load")
        settle(page)
        capture(page, "sources")

        # Source health is a clip of its own section: the page above it is the previous shot.
        health = page.evaluate("""() => {
            const heading = [...document.querySelectorAll('.eyebrow')].find(e => e.textContent.trim() === 'SOURCE HEALTH');
            const top = heading.closest('.section-heading').getBoundingClientRect().top + window.scrollY;
            const table = document.querySelector('.source-table-wrap').getBoundingClientRect();
            return {x: 0, y: top - 24, width: 1280, height: table.bottom + window.scrollY - top + 48};
        }""")
        page.screenshot(path=out / "source-health.png", clip=health, full_page=True)
        print("  source-health.png")

        page.goto(f"{base}/setup", wait_until="load")
        settle(page)
        # The wizard scrolls its current step into view on load; the shot wants the step bar.
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        capture(page, "setup-import")

        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=pathlib.Path, default=DEFAULT_OUT,
                        help="where the PNGs are written (default: docs/screenshots)")
    parser.add_argument("--data-dir", type=pathlib.Path, default=None,
                        help="keep the seeded installation here instead of a temporary directory")
    parser.add_argument("--port", type=int, default=8799, help="port for the throwaway server")
    arguments = parser.parse_args()

    with contextlib.ExitStack() as stack:
        data_dir = arguments.data_dir or pathlib.Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="rolebeacon-screenshots-"))
        )
        # Set before rolebeacon is imported anywhere: Settings reads the environment on load, and
        # the whole point is that this never opens the real application-data directory.
        os.environ["ROLEBEACON_DATA_DIR"] = str(data_dir.resolve())
        os.environ["ROLEBEACON_AUTO_SYNC"] = "0"
        settings = seed(data_dir.resolve())
        with serving(settings, arguments.port) as base:
            shoot(base, arguments.output_dir.resolve())
    print(f"wrote 9 screenshots to {arguments.output_dir}")


if __name__ == "__main__":
    main()
