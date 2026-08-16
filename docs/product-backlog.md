# Product backlog

Unscheduled product ideas that should remain visible during future planning. These are requirements notes, not commitments to a particular implementation.

The broader release assessment and prioritized defect list are recorded in the [Production-readiness review](production-readiness-review.md).

## User-configurable score distribution

Status: implemented in the web setup/settings flow and the shared-schema CLI import path. Zero is
allowed because eligibility is enforced by a separate deterministic gate; setting a factor to zero
removes only its influence on eligible-job ranking. An interactive prompt-by-prompt CLI remains part
of the broader setup-wizard item below.

Let the user distribute the 100 opportunity-fit points across the scoring factors that matter to them instead of requiring the built-in distribution.

Acceptance criteria:

- Provide a clear default distribution matching the current scoring behavior.
- Let users assign non-negative weights to the supported fit dimensions, with validation that the complete distribution totals 100.
- Expose the same capability in the web setup/settings flow and the CLI.
- Explain each factor and show how changing its weight affects ranking before saving.
- Store the configuration in a versioned user-owned profile or preference schema.
- Treat a weight change as a scoring-behavior change and requeue stale evaluations exactly once.
- Keep eligibility as a hard gate. A customized fit distribution must never override authorization, sponsorship, clearance, or explicit geographic restrictions.
- Keep `location_authorization` deterministic even if the user changes its share of the 100 points; a model must never supply its value.
- Preserve rules-only operation and deterministic repeatability for any valid distribution.

Open design question: decide whether advanced users may set a factor to zero or whether some deterministic dimensions require a minimum visible contribution. This must not weaken the separate eligibility gate.

## Job-detail score drill-down tooltips

Status: implemented as native expandable details for mouse, keyboard, and touch use, with canonical
factor definitions, maximums, scoring-provider disclosure, unknown-state wording, and gate/cap text.

Add accessible tooltips to every factor in the job-detail score breakdown so users can understand what the factor measures, why the displayed points were awarded, and what information was missing or limiting.

Acceptance criteria:

- Give every score factor a concise plain-language definition and show its maximum points.
- Explain which posting evidence and candidate-profile evidence contribute to the displayed value.
- Distinguish a confirmed mismatch from missing or unknown information; never imply that missing evidence proves the candidate lacks a qualification.
- Explain deterministic factors, especially location and authorization, without attributing them to a model.
- Identify relevant caps or gates, including the ineligible score cap, and make clear that fit cannot override eligibility.
- When LLM scoring is enabled, identify model-scored factors while keeping deterministic factors visibly separate.
- Keep tooltip text consistent with the canonical dimension metadata and scoring implementation so documentation cannot drift from behavior.
- Support mouse, keyboard, and touch interaction, with appropriate focus behavior and accessible names or descriptions for assistive technology.
- Keep essential explanations available without hover, such as through focus, tap, or an expandable details pattern.
- Add UI tests for tooltip presence, factor-to-explanation mapping, keyboard access, unknown-state wording, and rules-only versus LLM modes.

## Explain and align candidate-profile scoring inputs

Status: implemented in setup/settings copy and model disclosure, with contact fields excluded from
the compact scoring profile and regression coverage for both rules-only and model-assisted wording.

Correct the `CV & application profile` guidance that currently says its fields are used only for résumé generation and application preparation. Experience, projects, education, summary, and skills can be supplied to LLM job-fit scoring, while rules-only scoring currently uses headline, summary, and skills but not most detailed experience evidence. Contact details are application-only and must remain excluded from scoring prompts.

Acceptance criteria:

- Distinguish job-source discovery and collection from eligibility evaluation, fit scoring, ranking, and application generation.
- Label each candidate-profile field or section with its actual consumers: rules-only scoring, LLM scoring, résumé generation, cover-letter generation, browser preparation, or display only.
- State clearly that experience, projects, education, summary, and skills may affect LLM-generated dimensions, evidence, and gaps.
- Explain the narrower rules-only behavior until deterministic scoring consumes the same verified experience evidence.
- Identify contact fields as application-only and confirm that email, phone, and detailed contact links are excluded from scoring prompts.
- Show a concise disclosure before enabling an external or LAN model that lists the candidate-profile sections sent for job scoring.
- Keep the wording derived from, or tested against, the actual compact profile builders so UI copy cannot silently drift from implementation.
- Add tests for the rules-only and LLM explanations and for the exclusion of contact information from scoring prompts.

## Company fact-coverage evidence quality and drill-down

Status: implemented with a five-factor evidence checklist, distinct sampling/source metrics,
contextual extraction, token-aware engineering signals, redirect validation, and representative
sampling regression coverage.

Make company fact coverage auditable and resistant to evidence-extraction false positives. Posting volume and source count must remain separate from the number of distinct hiring facts actually established, but the UI should expose the calculation and the evidence behind every awarded point.

Acceptance criteria:

- Show the five coverage categories—remote policy, sponsorship, relocation, compensation, and engineering signals—with `established`, `contradicted`, or `unknown` states and their point contribution.
- Display job volume, job texts sampled, unique evidence sources, and verified official hiring-page types separately from fact coverage.
- Require explicit remote-work or work-location context before regional wording can establish remote policy; operational phrases such as project implementation `within the region` must not match.
- Match short skills such as `Go`, `C`, and `R` using token-aware rules so they cannot match employer names or unrelated words.
- Preserve and display supporting evidence per engineering signal instead of citing one sentence for an aggregated list of technologies.
- Validate the final URL, host, title, and page purpose after redirects before counting a source as careers, engineering, benefits, or another official hiring-page type.
- Replace the latest-20-only job evidence input with a bounded, deterministic, representative sample across locations, job families, and posting dates.
- Explain why repeated postings do not raise coverage when they establish no additional fact category.
- Add regression tests for contextual remote false positives, substring collisions, redirected non-careers pages, representative sampling, and per-signal citations.

The motivating Google investigation and its observed 4/10 calculation are recorded in the follow-up section of the [Production-readiness review](production-readiness-review.md#follow-up-investigation-google-fact-coverage-410).

## Profile-aware security-clearance eligibility

Status: implemented as a deterministic comparison of structured posting requirements with optional
candidate-owned policy. Missing, negated, preferred, and ambiguous wording remains unknown.

Replace the current blanket clearance rejection with a deterministic, evidence-backed comparison between a structured posting requirement and an optional candidate-owned clearance policy. Missing facts must remain unknown, negated and preferred wording must not become hard gates, and sensitive clearance information must be minimized and kept local.

The current behavior, risk analysis, proposed data models, decision matrix, implementation phases, and acceptance criteria are documented in [Security-clearance eligibility: gaps, risks, and recommended design](security-clearance-eligibility.md).

## Complete setup wizard in web UI and CLI

Provide a guided setup path that explicitly collects the information RoleBeacon requires and makes source selection a first-class step rather than something users must discover later.

The wizard should cover at least:

1. Candidate profile and experience information required for meaningful matching.
2. Target roles, preferred skills, preferred domains, and company preferences.
3. Mobility, work authorization, sponsorship needs, relocation willingness, and remote-work scope.
4. Score distribution, once user-configurable weighting is implemented.
5. Source selection, using the same catalog and enable/disable behavior as the Sources page.
6. A review screen that identifies missing or ambiguous critical information before activation.
7. Explicit activation and an explanation that external collection starts only after activation.

Web requirements:

- Include source selection directly in the first-run flow, with a link to the full Sources page for advanced management.
- Show which sources are enabled, what geographic or company coverage they provide, and whether they require credentials or user-owned alerts.
- Allow users to return to the wizard/checklist later from Settings.

CLI requirements:

- Provide an interactive setup command with the same required fields and validation as the web wizard.
- Provide scriptable/non-interactive flags or a documented configuration import path for headless use.
- Reuse the same schemas, source catalog, validation, and activation service as the UI; do not maintain a second interpretation of setup completeness.
- Show a final summary and require explicit confirmation before activation or first external sync.

Success means a new user can complete a valid rules-only setup, knowingly select sources, understand any missing critical facts, and start collection from either interface without first reading implementation documentation.

Implementation note: the scriptable CLI path is now `rolebeacon setup --from-json PATH [--activate]`
and reuses `SetupPayloadV1`, `SetupService`, the source catalog, and explicit activation. A separate
interactive terminal questionnaire is deferred: duplicating the mature accessible web controls in an
untested prompt flow would add a second partial setup interpretation. It should be implemented only
with terminal UX tests (including cancellation, secret input, source summaries, and final confirmation);
the documented import path provides complete headless setup without that duplication.
