# Product backlog

Unscheduled product ideas that should remain visible during future planning. These are requirements
notes, not commitments to a particular implementation. Completed work is documented in release history
and topic-specific design notes instead of remaining in this backlog.

The broader release assessment and prioritized defect audit are recorded in the
[Production-readiness review](production-readiness-review.md).

No open items. The interactive `rolebeacon setup` questionnaire, the last entry here, is implemented:
it reuses `SetupPayloadV1`, `SetupService.review` for completeness, the shared source catalog, and the
same activation service as the web wizard. Its activation prompt deliberately defaults to no, while the
web wizard's equivalent checkbox is pre-checked, because pressing Enter is a weaker statement of intent
than leaving a visible box ticked.
