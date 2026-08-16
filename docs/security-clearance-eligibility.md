# Security-clearance eligibility: gaps, risks, and recommended design

Implementation status (2026-08-16): the structured candidate policy, deterministic posting parser,
comparison matrix, exact evidence capture, and local-only persistence described here are implemented.
Clearance wording alone never makes a job ineligible; rejection requires an explicit requirement and
an explicit candidate-side conflict or user-owned exclusion. Unknown facts remain unknown.

Date: 2026-08-16

Status: implemented corrective design. The problem-analysis sections below preserve the pre-remediation
audit trail; the decision matrix and acceptance criteria describe the shipped deterministic behavior.
Recognition now requires security context, ambiguous mentions never gate eligibility, and only an
explicit posting requirement can be compared with an explicit candidate policy or user exclusion.

This note expands the clearance finding from the broader [Production-readiness review](production-readiness-review.md).

## Summary

RoleBeacon currently marks a job ineligible whenever its posting matches a small security-clearance regular expression. The resulting message says that the requirement conflicts with the configured eligibility profile, but neither the candidate profile nor the mobility profile contains clearance information. The system therefore assumes a conflict it cannot establish.

Until RoleBeacon has both a structured posting requirement and an explicit candidate-side fact, a clearance mention should normally produce `unknown`, not `ineligible`. A user-owned excluded phrase may still impose a deliberate hard rejection.

## Current behavior

[`evaluate_eligibility`](../src/rolebeacon/scoring.py) builds one text value from the job title, company, location, remote scope, and description. It then recognizes clearance with this expression:

```regex
(?:active |ability to obtain )?(?:security |secret |top secret )clearance
```

Any match reaches the clearance branch before work authorization, sponsorship, relocation, or remote-scope checks and produces:

```text
Security clearance conflicts with the configured eligibility profile
```

The outcome is a hard gate:

- Eligibility becomes `ineligible`.
- The location-and-authorization dimension receives zero points.
- The rules score is capped at the configured ineligible ceiling.
- The verdict becomes `reject`.
- Optional LLM scoring is not invoked, so a model cannot correct the decision.

This is deterministic behavior, but it is not currently profile-aware.

## Information that currently contributes to eligibility

The eligibility gate uses:

- Job title, company, description, location, and remote scope.
- Collector-provided visa-sponsorship and relocation signals.
- Configured work-authorized countries.
- Relocation targets and whether sponsorship is required outside authorized countries.
- The remote-from-current-country preference.
- Company blocklists and user-owned excluded phrases.
- Generated country, remote, priority-company, and watchlist strategies.

The following information does not currently contribute to eligibility:

- Candidate clearance status, jurisdiction, level, or expiration.
- Candidate citizenship or nationality.
- Willingness or eligibility to undergo vetting.
- Skills, experience, target role, seniority, or salary; these do not determine eligibility status. Headline, summary, and skills affect rules-only fit scoring, while experience, projects, education, summary, and skills can also affect LLM fit scoring.
- Company research about sponsorship or relocation.
- Contractor and employer-of-record preferences.

## Gaps and risks

### 1. The reported profile conflict is not established

There is no clearance field to compare with the posting. The current message overstates what RoleBeacon knows and can make a deterministic result appear more authoritative than it is.

Risk: potentially suitable jobs are rejected without a supporting candidate fact, and the user receives no actionable explanation.

### 2. Negated and non-mandatory wording can cause false rejection

The expression finds a clearance phrase without understanding whether it is negated, optional, or merely discussed. Examples include:

- `No security clearance is required.`
- `This role does not require security clearance.`
- `Security clearance is preferred but not required.`
- Descriptive text about customers, products, or processes that mentions clearances without imposing a candidate requirement.

Risk: the job becomes ineligible even though the posting explicitly says the opposite.

### 3. Important requirement forms are missed

The recognizer does not reliably cover:

- Abbreviations such as `TS/SCI` or jurisdiction-specific clearance names.
- Public-trust or government-suitability requirements.
- Phrases such as `cleared candidate` or a bare `clearance required`.
- Hyphenated forms such as `security-clearance` or `top-secret`.
- Export-control and citizenship restrictions, which may affect eligibility but are not the same as a clearance.
- Requirements written in languages other than English.

Risk: material restrictions can remain `unknown` or be missed entirely while superficially similar English wording is hard-rejected.

### 4. Distinct requirement types are conflated

The current rule does not distinguish between:

- An active clearance required on the start date.
- Eligibility or ability to obtain a clearance after hiring.
- Employer-supported clearance processing.
- A preferred qualification.
- An ordinary background check.
- Citizenship, export-control, or residency restrictions.
- An explicit statement that no clearance is required.

Risk: RoleBeacon cannot select the correct outcome because these cases need different candidate facts and different decision rules.

### 5. Clearance jurisdiction and level are not modeled

A clearance is not a single worldwide credential. A useful comparison may require jurisdiction, scheme, level, status, and expiration. The posting may omit some of these facts.

Risk: a generic `has clearance` flag could create false eligibility just as easily as the current generic rule creates false ineligibility.

### 6. The evidence is not auditable

The stored risk contains only a generic sentence. It does not preserve the matched phrase, requirement classification, source field, or enough surrounding context to explain why the rule fired.

Risk: users and developers cannot efficiently verify or correct a decision, and regression reports lack the evidence needed for diagnosis.

### 7. Sensitive-data handling needs an explicit policy

Clearance and citizenship-related facts can be sensitive. Adding them to the profile without data minimization would expand the privacy impact of backups, logs, generated prompts, exports, and diagnostics.

Risk: implementation of a correctness feature could unnecessarily increase exposure of sensitive candidate data.

### 8. Regression coverage is insufficient

The scoring tests exercise user-owned excluded phrases but do not directly cover the automatic clearance detector, negation, optional requirements, jurisdiction-specific terminology, or candidate-side clearance states.

Risk: changes to wording or rule precedence can silently reintroduce false rejection or false acceptance.

### 9. Adjacent eligibility rules can compound the result

Clearance classification operates inside a broader gate that also has known edge cases:

- Relocation assistance must not substitute for required visa sponsorship.
- Missing remote geography must remain unknown rather than becoming worldwide.
- Country aliases must not create false location matches or misses.
- Company blocklist and excluded-phrase matching must avoid accidental substring matches.
- Unknown facts must remain unknown instead of being inferred from company reputation or general fit.

Risk: correcting clearance alone is not sufficient if another branch can still produce an unsupported hard decision.

## Recommended decision principles

1. A hard rejection requires an explicit posting requirement and an explicit candidate-side conflict.
2. Missing candidate or posting facts produce `unknown`, not `ineligible` or `eligible`.
3. Negation and `preferred` wording must be evaluated before a requirement can become a gate.
4. Clearance, citizenship, export control, work authorization, sponsorship, and background checks remain separate facts.
5. Every decision preserves the exact posting evidence used by the rule.
6. Rules-only operation remains complete and deterministic; an LLM may not determine clearance eligibility.
7. Sensitive candidate facts are optional, local-only, excluded from logs, and sent to a model only when strictly necessary and explicitly allowed.

## Recommended posting requirement model

Parse a posting into a structured result rather than a single Boolean:

```json
{
  "kind": "active_required",
  "jurisdiction": "US",
  "scheme": "DoD",
  "level": "Secret",
  "evidence": "Active Secret clearance required",
  "source_field": "description",
  "confidence": "explicit"
}
```

Supported `kind` values should include at least:

- `not_required`
- `preferred`
- `ability_to_obtain`
- `active_required`
- `background_check`
- `citizenship_or_export_control`
- `ambiguous`
- `not_mentioned`

The parser should return multiple requirements when the posting contains separate constraints. It must not silently turn an ambiguous phrase into an explicit requirement.

## Recommended candidate policy model

The minimum useful profile can avoid collecting detailed credentials:

```json
{
  "clearance_policy": {
    "status": "unknown",
    "willing_to_undergo_vetting": null,
    "explicitly_excluded_requirements": []
  }
}
```

Possible `status` values:

- `unknown`
- `cannot_meet`
- `eligible_to_attempt`
- `has_active_clearance`

If users need precise matching, credentials can be an optional extension:

```json
{
  "jurisdiction": "US",
  "scheme": "DoD",
  "level": "Secret",
  "status": "active",
  "expires_on": null
}
```

RoleBeacon should never infer these values from location, nationality, work authorization, résumé text, or company research.

## Recommended outcome matrix

| Posting requirement | Candidate fact | Eligibility effect |
| --- | --- | --- |
| Not required | Any | Continue to the remaining eligibility rules |
| Preferred | Unknown or unmet | Continue; show a non-gating gap |
| Active clearance required | Matching active credential | Continue; still verify jurisdiction and level |
| Active clearance required | Explicitly cannot meet | Ineligible |
| Active clearance required | Unknown | Unknown |
| Ability to obtain | Explicitly cannot or will not undergo vetting | Ineligible |
| Ability to obtain | Eligible or willing, but not verified | Unknown pending confirmation |
| Ability to obtain | Unknown | Unknown |
| Ambiguous mention | Any | Unknown with the matched evidence shown |
| User-owned exact exclusion | Any | Ineligible by explicit user policy |

Clearance should affect only its own eligibility evidence. It must not establish work authorization, sponsorship, citizenship, or geographic eligibility.

## Recommended user-facing messages

For an unconfigured or ambiguous case:

```text
The posting may require a security clearance. Your clearance eligibility is not configured; verify the exact jurisdiction and requirement.
```

For a confirmed conflict:

```text
The posting requires an active US Secret clearance, and your profile explicitly says this requirement cannot be met.
```

For a user-owned exclusion:

```text
Rejected by your excluded requirement: active security clearance required.
```

Every message should display or link to the matched posting excerpt.

## Implementation sequence

### Phase 1: safe correction

- Make automatic clearance mentions `unknown` rather than `ineligible`.
- Recognize common negated and preferred forms before positive requirement matching.
- Replace the current conflict message with an honest unknown-state message.
- Preserve explicit user-owned excluded phrases as hard gates.
- Add regression tests before changing the rules.

### Phase 2: structured extraction

- Introduce the posting requirement model and deterministic classifiers.
- Store the matched evidence and classification with the eligibility evaluation.
- Keep citizenship, export control, ordinary background checks, and clearance as separate categories.
- Treat unsupported languages and ambiguous terminology as unknown.

### Phase 3: optional candidate configuration

- Add a versioned, optional clearance policy to the profile schema.
- Expose it in web and CLI setup with plain-language privacy guidance.
- Keep the default `unknown`; never infer a value during résumé import.
- Provide explicit controls for clearing or removing sensitive information.

### Phase 4: decision integration and re-evaluation

- Implement the outcome matrix as deterministic rules.
- Increment the scoring-behavior version so stale jobs are evaluated exactly once.
- Ensure an LLM cannot set or override the clearance result.
- Re-evaluate existing jobs and expose why their status changed.

### Phase 5: privacy and operational controls

- Keep clearance details in the ignored operating-system application-data directory.
- Exclude them from normal logs, telemetry, exception messages, and support bundles.
- Minimize model prompt disclosure and require explicit configuration before including details.
- Document deletion, export, backup, and retention behavior.

## Required tests

At minimum, add deterministic tests for:

- Explicit active-clearance requirements.
- Ability-to-obtain requirements.
- Negated requirements.
- Preferred but not required wording.
- Ambiguous and contextual mentions.
- Hyphenated wording and common abbreviations.
- Jurisdiction and level matches and mismatches.
- Unknown candidate clearance status.
- Explicit candidate inability and explicit user exclusions.
- Multiple constraints in one posting.
- Non-English or unrecognized wording remaining unknown.
- Rule precedence relative to authorization, sponsorship, relocation, blocklists, and remote geography.
- Identical rules-only results across repeated evaluations.

## Acceptance criteria

- No clearance mention becomes a hard rejection without a proven candidate-side conflict or explicit user-owned exclusion.
- `No security clearance is required` never triggers a clearance restriction.
- Preferred requirements do not become hard gates.
- Ambiguous or missing facts remain visible as unknown.
- Every clearance result contains the exact supporting posting evidence.
- Clearance cannot be overridden by fit, company reputation, or a model.
- Sensitive profile fields are optional, local-only, removable, and absent from logs by default.
- Changed clearance behavior requeues stale evaluations exactly once.
- Web and CLI surfaces explain the same decision consistently.
