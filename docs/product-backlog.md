# Product backlog

Unscheduled product ideas that should remain visible during future planning. These are requirements
notes, not commitments to a particular implementation. Completed work is documented in release history
and topic-specific design notes instead of remaining in this backlog.

The broader release assessment and prioritized defect audit are recorded in the
[Production-readiness review](production-readiness-review.md).

## Interactive CLI setup questionnaire

Status: deferred. The complete accessible web setup flow and the schema-driven headless import path
are implemented.

Add an interactive `rolebeacon setup` questionnaire only when it can reuse the same schemas, source
catalog, validation, credential boundaries, and activation service as the web wizard.

Acceptance criteria:

- Collect the candidate profile, mobility and authorization facts, preferences, score distribution,
  source selection, and optional model configuration without maintaining a second interpretation of
  setup completeness.
- Support cancellation at every stage without saving a partial generation.
- Treat secrets and credential-file paths as private terminal input and never echo them.
- Explain source coverage and credential requirements.
- Show a final summary, identify missing or ambiguous critical facts, and require explicit confirmation
  before activation or the first external sync.
- Add terminal UX coverage for navigation, cancellation, invalid input, secret input, source summaries,
  and final confirmation.

Until then, `rolebeacon setup --from-json PATH [--activate]` remains the complete headless path. It uses
`SetupPayloadV1`, `SetupService`, the shared source catalog, and explicit activation.
