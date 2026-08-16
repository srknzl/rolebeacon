# Production-readiness review

Date: 2026-08-16

## Verdict

The reviewed revision was not ready for production. No Tier 0 emergency vulnerability was identified,
but the Tier 1 correctness and data-integrity defects below blocked release. The remediation branch now
resolves every Tier 1 finding with regression coverage and fixes every Tier 2 finding. The original
finding descriptions remain below as the audit trail; the resolution ledger records the resulting
behavior that must be preserved in review.

This report records the findings from the pre-production code review. The clearance-specific corrective design is documented separately in [Security-clearance eligibility: gaps, risks, and recommended design](security-clearance-eligibility.md).

## Remediation ledger

| Findings | Resolution |
| --- | --- |
| 1–3 | Sponsorship and relocation are independent; clearance uses a structured candidate conflict matrix; absent remote geography remains unknown. |
| 4–5 | Non-truncated complete snapshots reconcile source associations, and eligibility signals participate in content identity while mutable links update independently. |
| 6–7 | Setup round-trips all values with explicit secret preserve/replace/remove actions; migration dispatches before destination initialization. |
| 8–10 | Company lists use Unicode exact identity and explicit aliases; company keys are Unicode-safe; ambiguous opening collisions enter duplicate review instead of auto-merging. |
| 11 | Source preview, manual refresh, and scheduled refresh all require explicit activation. |
| 12–15 | Ineligible jobs cannot receive company-score blending; country aliases, salary hard filters, review limits, and complete verified-profile vocabulary are enforced. |
| 16–17 | Official-domain comparison uses the Public Suffix List and validates redirects; model results require exact keys, bounds, totals, official citation membership, and evidence coverage. |
| 18–20 | Mutations use exact-origin plus CSRF protection; Gmail tokens use injected app-data storage with owner-only atomic writes; duplicate merge refuses to discard competing user artifacts. |
| 21–24 | FTS input is literal-safe; setup persistence uses atomic files and an atomic generation manifest; synchronization has a process lock; pagination truncation is recorded, displayed, and excluded from closure. |
| 26–29 | External URL schemes are allow-listed, startup rewrites only changed rows, version/docs derive from canonical values, and partial source failures finish as `completed_with_errors`. |
| 30 | CI audits the exported locked runtime dependency set and publishes a CycloneDX SBOM; `SECURITY.md` defines vulnerability and dependency-update handling. |

Finding 25 is partially resolved and deliberately deferred as Tier 3 follow-up. All payloads now reject
invalid JSON and non-object bodies consistently, affected boolean and integer mutations reject coercion,
and malformed values return 422 instead of 500. Converting every mutation to exported typed request
models is deferred because that changes the public OpenAPI contract across setup, imports, source
discovery, feedback, and duplicate review. It should land as a separately versioned API change with
consumer compatibility tests; performing that broad contract rewrite inside a correctness remediation
would add release risk without leaving a known coercion or crash path from this review.

## Tier 1: release blockers

### 1. Relocation support is incorrectly treated as visa sponsorship

[`evaluate_eligibility`](../src/rolebeacon/scoring.py) marks a target-country job eligible when either sponsorship or relocation support is present. For a country strategy with `requires_sponsorship=true`, relocation assistance can therefore be treated as sufficient even when visa sponsorship is unavailable or unknown.

Impact: a candidate can be told they are eligible for a role whose legal work-authorization requirement is not satisfied.

Recommendation: model relocation and sponsorship as separate facts. A sponsorship-required strategy must require affirmative sponsorship evidence; relocation support may be reported as an additional benefit but must never substitute for sponsorship.

### 2. Clearance wording can cause unsupported hard rejection

The clearance expression in [`scoring.py`](../src/rolebeacon/scoring.py) matches clearance wording without comparing it with any candidate clearance setting. It also does not handle negation. For example, `No security clearance is required` is currently classified as ineligible.

Impact: suitable jobs can be rejected even when the posting explicitly states that clearance is unnecessary.

Recommendation: automatic clearance mentions should remain unknown until RoleBeacon has both an explicit posting requirement and a candidate-side conflict. Implement the detailed design in [security-clearance-eligibility.md](security-clearance-eligibility.md).

### 3. Missing remote geography is converted into worldwide eligibility

The RemoteOK and Himalayas collectors in [`collectors.py`](../src/rolebeacon/collectors.py) use `Worldwide` when relevant location restrictions are absent.

Impact: missing geography becomes affirmative worldwide evidence and can make a candidate appear eligible for a country-restricted job.

Recommendation: preserve absent geography as unknown. Only set worldwide scope when the source supplies explicit worldwide evidence.

### 4. Closed or removed jobs never become inactive

[`CollectionBatch`](../src/rolebeacon/domain.py) exposes `complete_snapshot` and `provider_total`, and some collectors populate them, but [`sync.py`](../src/rolebeacon/sync.py) does not reconcile jobs missing from a completed snapshot. Normal collection does not deactivate disappeared postings.

Impact: expired opportunities remain active indefinitely and pollute ranking, review queues, and application decisions.

Recommendation: define reliable per-adapter snapshot semantics. Reconcile source associations transactionally, then mark a job inactive only when no active source still reports it. Partial, paginated, or date-bounded responses must not claim to be complete snapshots.

### 5. Eligibility metadata and application links can remain stale

The content digest in [`database.py`](../src/rolebeacon/database.py) excludes structured eligibility signals, including sponsorship and relocation, and excludes application and canonical URLs. The canonical job record is replaced only when that digest changes.

Impact: a collector can correct sponsorship information or application links without triggering an update or re-evaluation.

Recommendation: hash canonical fields that affect eligibility and scoring. Update links and other mutable canonical metadata independently, avoiding unnecessary scoring churn for irrelevant source metadata.

### 6. Saving settings silently destroys existing configuration

The settings flow is not lossless:

- Saved LLM API keys are omitted from setup responses, the input remains blank, and the blank is written over the stored secret.
- Relocation cities are reset.
- Salary preferences, review limit, sponsorship policy, and timezone can be replaced with hard-coded defaults.
- The visible skills input can be ignored when hydrated detailed skills are present.

Relevant code is in [`setup.py`](../src/rolebeacon/setup.py), [`config.py`](../src/rolebeacon/config.py), and [`setup.html`](../src/rolebeacon/resources/templates/setup.html).

Impact: a routine preferences edit can erase secrets and change eligibility or ranking behavior without warning.

Recommendation: implement lossless round-tripping and server-side merge semantics. Treat secret inputs as unchanged unless the user explicitly replaces or removes them, and add round-trip tests for every setup field.

### 7. The documented database migration command cannot migrate

[`cli.py`](../src/rolebeacon/cli.py) initializes the destination database before dispatching commands. [`migration.py`](../src/rolebeacon/migration.py) skips migration whenever the destination exists.

Impact: a migration targeting a fresh path reports that the destination already exists and never imports the legacy database.

Recommendation: run migration before normal destination initialization or distinguish a newly initialized empty database from a populated destination. Add a CLI-level migration test.

### 8. Blocklist matching rejects unrelated companies

Company matching in [`scoring.py`](../src/rolebeacon/scoring.py) removes punctuation and then treats the configured value as a substring of the employer name. This makes `Meta` match `Metabase` and `Go` match `Google`.

Impact: valid opportunities can be hard-rejected by an unintended company match. Priority and watchlist routing can also be misapplied.

Recommendation: use normalized equality and explicit user-owned aliases. Do not use arbitrary substring matching for a hard gate.

### 9. Non-Latin employer names can collapse into one company

Company normalization in [`database.py`](../src/rolebeacon/database.py) strips everything outside ASCII letters and digits. Employer names written entirely in scripts such as Japanese or Chinese normalize to an empty string. The normalized company name is unique.

Impact: unrelated companies can be overwritten, conflated, or used as the same deduplication identity.

Recommendation: use Unicode normalization and case folding. Reject empty normalized identities and add multilingual company-identity tests.

### 10. Strong deduplication can silently merge distinct openings

[`database.py`](../src/rolebeacon/database.py) treats exact normalized company, title, and location as a strong identity. When dates or requisition identifiers are missing, the compatibility checks are permissive.

Impact: two distinct openings with a common title at the same employer and location can merge without review, replacing description or source information.

Recommendation: require a stable external identifier, a canonical URL relationship, or stronger corroborating evidence before automatic merging. Send ambiguous exact-title collisions to the duplicate-review queue.

### 11. External requests can occur before setup activation

Source discovery in [`app.py`](../src/rolebeacon/app.py) does not enforce activation. Manual synchronization checks setup completion but bypasses the `activated` flag through [`sync.py`](../src/rolebeacon/sync.py).

Impact: the implementation conflicts with the product boundary that external sources are not contacted before explicit activation.

Recommendation: enforce activation consistently. If an explicit preview or manual refresh is intended to count as consent, update the invariant, UI, tests, and documentation to state that behavior precisely.

## Tier 2: high-priority correctness, security, and reliability

### 12. Company fit can raise the displayed score of an ineligible job above its cap

[`database.py`](../src/rolebeacon/database.py) blends company fit into opportunity score regardless of eligibility. An ineligible job score capped at 39 and a company score of 100 can produce a displayed opportunity score of 51.

Impact: the headline score suggests that company reputation overcame the hard gate even though eligibility status remains ineligible.

Recommendation: do not blend company fit into ineligible or unresolved opportunities. Continue to display company fit separately if useful.

### 13. Country and territory aliases are incomplete

Location matching in [`scoring.py`](../src/rolebeacon/scoring.py) contains only a small hand-written alias set. Common variants such as `Turkey` for the `Türkiye` display name and `UK` can remain unmatched.

Impact: authorized or relevant jobs can incorrectly remain unknown.

Recommendation: introduce a maintained, tested country and territory alias table, keeping short-code matching conservative to avoid substring false positives.

### 14. Advertised preference fields are not enforced

[`profile.py`](../src/rolebeacon/profile.py) exposes `salary.hard_filter` and `daily_review_limit`, but they are not applied end to end. The dashboard uses a fixed review count. Salary scoring in [`scoring.py`](../src/rolebeacon/scoring.py) can also award full points when the posting currency is not comparable with the candidate preference.

Impact: the product accepts configuration that does not control behavior as users reasonably expect.

Recommendation: implement the settings fully or remove them from the public schema and UI until supported. Treat unknown currency comparison as unknown, not a confirmed match.

### 15. Rules-only scoring ignores much of the verified candidate record

Candidate terms in [`scoring.py`](../src/rolebeacon/scoring.py) are built mainly from headline, summary, and skill fields. Technologies and evidence stored in experience, projects, and education do not contribute to deterministic matching.

At the same time, the settings page says that all fields in the `CV & application profile` section do not influence job discovery and are used only for generated résumés or applications. That statement is too broad. In LLM mode, [`llm.py`](../src/rolebeacon/llm.py) sends summary, location, experience, projects, skills, education, and search preferences to the job-fit scorer. Those fields can affect role, stack, domain-experience, and seniority points as well as the evidence and gaps shown to the user. Contact details remain excluded from scoring.

Impact: capabilities already recorded in the candidate profile can appear missing in rules-only mode, while the UI incorrectly tells LLM-mode users that the same experience fields cannot affect scoring. Users cannot make an informed decision about profile completeness or model disclosure.

Recommendation: derive a normalized evidence vocabulary from all verified candidate sections while preserving provenance for explanations. Replace the blanket UI statement with field-level guidance that distinguishes source collection, eligibility, deterministic scoring, LLM scoring, and application-material generation. Explicitly identify contact fields as application-only and disclose which non-contact profile fields are sent to a configured model.

### 16. Official-domain validation is unsafe for multi-label public suffixes

[`company.py`](../src/rolebeacon/company.py) assumes the registrable domain is always the final two labels. For example, `careers.example.co.uk` is reduced to `co.uk`. Redirect destinations are stored without revalidating the final host.

Impact: unrelated sites under a shared public suffix can be accepted as if they were the same official domain.

Recommendation: use the Public Suffix List and revalidate the final redirect origin before accepting official evidence.

### 17. Model output is trusted beyond what runtime validation proves

Company research in [`company.py`](../src/rolebeacon/company.py) requests evidence URLs, but runtime checks do not verify that citations belong to fetched official evidence or support the returned claims. Job scoring also relies heavily on remote schema adherence.

Impact: malformed, fabricated, or out-of-range model output can be persisted and affect ranking.

Recommendation: treat every model response as untrusted. Validate exact keys, dimension bounds, totals, citation membership, and evidence coverage locally.

### 18. The local-origin guard ignores scheme and port

The origin check in [`app.py`](../src/rolebeacon/app.py) accepts requests based only on hostname. An unrelated application hosted on another localhost port can submit cross-origin forms to mutating RoleBeacon endpoints.

Impact: the browser may send an unauthorized state-changing request even though the hostile page cannot read its response.

Recommendation: match exact configured origins, including scheme and port, and add CSRF tokens to browser-facing mutation endpoints.

### 19. Gmail OAuth tokens use the wrong storage location and weak file handling

The Gmail collector in [`collectors.py`](../src/rolebeacon/collectors.py) defaults token storage relative to the installed package rather than the configured operating-system application-data directory. It also relies on ambient umask permissions.

Impact: tokens can be placed in a repository, fail to persist in an installed wheel, or receive broader file permissions than intended.

Recommendation: inject the configured application-data path and create credential files with explicit owner-only permissions.

### 20. Duplicate merging can delete application artifacts

When both duplicate jobs already have application records, [`database.py`](../src/rolebeacon/database.py) deletes the losing application instead of reconciling résumé, cover letter, packet, and notes.

Impact: user-created artifacts can become orphaned or inaccessible.

Recommendation: preserve both sets of artifacts and require review when both duplicate candidates contain user work.

### 21. Valid search input can crash the JSON API

Search text is passed directly to SQLite FTS `MATCH` in [`database.py`](../src/rolebeacon/database.py). Input such as an unmatched quote raises an SQLite operational error. The HTML route handles search failures, but the JSON jobs endpoint in [`app.py`](../src/rolebeacon/app.py) does not.

Impact: ordinary user input can produce an HTTP 500 response.

Recommendation: escape searches as literal terms or parse them into a safe FTS expression. Return a validation response for intentionally supported advanced syntax errors.

### 22. Configuration persistence is non-atomic

[`config.py`](../src/rolebeacon/config.py) writes JSON files directly, and setup saves multiple related files sequentially.

Impact: a crash, disk-full condition, or concurrent read can expose truncated JSON or a mixed configuration generation.

Recommendation: write temporary files, flush and sync them, and finish with an atomic replacement. Use a shared generation identifier if several files must represent one setup transaction.

### 23. Synchronization locking works only inside one process

[`sync.py`](../src/rolebeacon/sync.py) uses an `asyncio.Lock`. A CLI refresh and the server scheduler can therefore run simultaneously. Startup recovery can mark another process's live run as stale in [`database.py`](../src/rolebeacon/database.py).

Impact: providers can be called twice, state can race, and active runs can be misreported as failed.

Recommendation: add a database or operating-system lock with owner identity and lease or heartbeat semantics.

### 24. Several collectors can silently truncate large job boards

Lever uses a fixed maximum without complete pagination, while Workday and SmartRecruiters use hard-coded page ceilings in [`collectors.py`](../src/rolebeacon/collectors.py). The source can still report success.

Impact: users receive incomplete coverage without a visible warning, and a truncated response could be mistaken for a complete snapshot.

Recommendation: respect source pagination configuration, expose truncation in source status, and prevent truncated responses from participating in absence-based deactivation.

## Tier 3: hardening and maintainability

### 25. API request validation is inconsistent

Manual dictionary parsing allows non-object payloads to reach code that assumes `.get`, numeric conversions can become HTTP 500 responses, and string values such as `"false"` can be coerced to true.

Recommendation: use typed request models and consistent validation responses for every API mutation.

### 26. External URL schemes are not allow-listed

Manual job imports accept any non-empty URL, which can later be rendered or passed to browser preparation.

Recommendation: normalize URLs at ingestion and allow only `https`, plus `http` where explicitly required.

### 27. Startup performs an unnecessary full-table rewrite

Database initialization in [`database.py`](../src/rolebeacon/database.py) updates every job, triggering full-text-search delete and insert operations.

Recommendation: limit compatibility rewrites to records that actually need migration and record completed schema/data migrations.

### 28. Version and documentation values have drifted

[`__init__.py`](../src/rolebeacon/__init__.py) reports version `0.1.0`, while [`pyproject.toml`](../pyproject.toml) builds `0.2.0`. The README scoring table also differs from the actual dimension constants in [`scoring.py`](../src/rolebeacon/scoring.py).

Recommendation: derive runtime version from installed package metadata and test documented scoring tables against the canonical constants.

### 29. Partial source failures look like successful refreshes

Individual collector errors are counted, but the overall run can still finish in the `complete` phase.

Recommendation: introduce a `completed_with_errors` state, surface failed-source counts prominently, and emit structured operational metrics.

### 30. Dependency security auditing is not a continuous gate

The review-time dependency audit was clean, but dependency auditing is not yet a continuous lockfile gate.

Recommendation: run a dependency vulnerability audit in CI, generate an SBOM for releases, and document dependency-update and vulnerability-response expectations.

## Follow-up investigation: Google fact coverage 4/10

The Google company profile was inspected after the initial review because it displayed `fact coverage 4/10` despite reporting that 100 collected job postings informed the assessment.

### Current calculation

Fact coverage is not calculated from posting or source count. [`CompanyResearchService._fact_coverage`](../src/rolebeacon/company.py) awards two points for each of five fact categories that is considered established:

1. Remote policy.
2. Visa sponsorship.
3. Relocation support.
4. Compensation stated in collected jobs.
5. Engineering signals.

For the inspected Google profile, the stored calculation was:

| Fact category | Stored result | Coverage points |
| --- | --- | ---: |
| Remote policy | Regional | 2 |
| Sponsorship | Unknown | 0 |
| Relocation | Unknown | 0 |
| Compensation | No structured salary found | 0 |
| Engineering signals | Present | 2 |
| **Total** |  | **4/10** |

The company-fit total of 63 consisted of domain alignment 18, engineering environment 20, location and mobility 8, compensation 5, company quality 8, and fact coverage 4.

### Evidence inspected

The local data contained:

- 100 jobs loaded by [`Database.company_jobs`](../src/rolebeacon/database.py).
- No recognized or broader visa, sponsorship, immigration, work-authorization, relocation, salary, compensation, pay-range, or base-pay wording across those 100 postings.
- 23 stored evidence records: 20 current job postings and 3 claimed official page types.
- No job with a structured minimum or maximum salary.

Repeated postings correctly do not increase coverage merely by repeating the same established fact. However, [`_job_evidence`](../src/rolebeacon/company.py) sends only the 20 most recent jobs into policy and engineering extraction. Up to 100 jobs are loaded, but all 100 are used only for the structured salary check and the summary count. This hidden sampling difference can miss facts for other employers even though it did not change the Google sponsorship, relocation, or compensation result in this dataset.

### Confirmed evidence-quality gaps

#### Remote policy was inferred from unrelated wording

The regional-remote result cited a sentence about taking ownership of project implementation `within the region`. The sentence did not describe remote work. The remote parser accepts generic `within the region` wording without requiring nearby remote, work-location, employment, or hiring context.

Impact: Google received two fact-coverage points and a regional-remote risk from evidence that did not establish a remote policy. Based on the displayed evidence, defensible coverage may be 2/10 rather than 4/10.

Recommendation: require remote-work or work-location context within the same sentence or a narrow token window around regional wording. Add negative tests for operational phrases such as implementation, ownership, sales, or service delivery `within the region`.

#### Engineering evidence can match substrings and cite the wrong sentence

The engineering claim listed `Java, Go, Python, Distributed systems` but cited a location sentence beginning with `Google`. Skill detection uses substring matching, so the skill `Go` can match the beginning of `Google`. Aggregated claims can also cite one sentence even when several claimed technologies were found in different sources.

Impact: the engineering category may be reasonable across the broader job set, but the displayed quote does not support the complete claim and gives the user no way to determine which source established each technology.

Recommendation: match skills with token or phrase boundaries, retain evidence independently for each signal, and render one or more exact supporting quotes instead of attaching an aggregated claim to a single convenient source.

#### An unrelated Google product page was classified as careers evidence

One stored official source was titled `Job Search on Google - Get Your Job Postings on Google Today`. This is a product page for employers publishing jobs into Google Search, not Google's own employment careers page. A conventional `/careers` request redirected there, and RoleBeacon retained the requested `careers` source type without validating the final URL or page purpose.

Impact: the page reports three official evidence types even though one claimed careers source is not useful employer-policy evidence. It did not add a fact-coverage point in this case, but it inflates the apparent provenance breadth.

Recommendation: validate final redirect hosts, paths, titles, and page purpose before retaining a requested source type. A page rejected as careers evidence may still be stored as non-hiring official evidence if it is otherwise relevant, but it must not establish hiring facts or official hiring-source breadth.

### Product recommendations

- Keep fact coverage independent from raw posting count; redundant sources should not create confidence.
- Show the five-factor checklist directly: remote, sponsorship, relocation, compensation, and engineering.
- Display source breadth separately from fact coverage, including jobs considered, job texts sampled, unique sources, and verified official hiring-page types.
- Use a representative deterministic job sample across locations and job families instead of only the latest 20 postings.
- Show the exact evidence behind each fact and distinguish established, contradicted, and unknown states.
- Do not call a source type official hiring evidence until the final fetched page has been validated for that purpose.
- Add regression fixtures for substring collisions, contextual remote-language false positives, redirect reclassification, sample coverage, and per-signal citations.

## Verification baseline

The following checks passed during the review:

- Ruff static analysis.
- Mypy type checking for `src/rolebeacon`.
- Pytest: 192 passed and 1 explicitly skipped model evaluation.
- Source distribution and wheel builds.
- Byte-for-byte comparison of `AGENTS.md` and `CLAUDE.md`.
- Dependency audit with no known vulnerabilities reported.

The passing suite demonstrates a good engineering baseline, but it lacks adversarial coverage for several semantic failure modes identified above.

## Recommended remediation order

1. Correct the deterministic eligibility false decisions: sponsorship versus relocation, clearance, remote geography, company blocklist identity, and country aliases.
2. Make settings round-trips lossless and fix migration before asking users to trust stored configuration or upgrades.
3. Repair canonical job updates, job closure reconciliation, Unicode company identity, and automatic deduplication before collecting long-lived production data.
4. Protect application artifacts and make configuration writes and sync ownership safe across failures and processes.
5. Harden evidence provenance, API validation, URL handling, origin checks, OAuth token storage, and model-output validation.
6. Add regression tests for every confirmed defect before changing scoring behavior, increment the scoring version, and re-evaluate stale jobs exactly once.

Production release should remain blocked until all Tier 1 findings have fixes and regression tests. Tier 2 issues involving credential storage, artifact loss, origin protection, configuration atomicity, and concurrency should also be resolved before broad external use.
