# Product backlog

Unscheduled product ideas that should remain visible during future planning. These are requirements notes, not commitments to a particular implementation.

## User-configurable score distribution

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
