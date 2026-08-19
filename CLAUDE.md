> **Instruction-file sync:** `CLAUDE.md` and `AGENTS.md` must remain byte-for-byte
> identical. Update both files in the same change and verify them with `cmp` or an
> equivalent cross-platform comparison.

# RoleBeacon — local-first job discovery and application assistant

RoleBeacon collects public job postings, evaluates eligibility before fit, ranks
opportunities against a user-owned candidate profile, and prepares application
artifacts for human review. Rules-only mode is a complete supported product mode;
an LLM must never be required for collection, deterministic scoring, or setup.

## Architecture

- `src/rolebeacon/collectors.py` contains public ATS and feed adapters.
- `src/rolebeacon/sync.py` owns startup catch-up, overlap windows, source isolation, and scoring.
- `src/rolebeacon/scoring.py` owns deterministic eligibility and generated-strategy ranking.
- `src/rolebeacon/profile.py` owns versioned public candidate, mobility, and preference schemas.
- `src/rolebeacon/setup.py` owns first-run setup and optional local-model assistance.
- `src/rolebeacon/company.py` owns provenance-backed employer research and company fit.
- `src/rolebeacon/services.py` owns résumé, cover-letter, and application artifacts.
- `src/rolebeacon/browser.py` may prepare fields but must never submit an application.

## Conventions

- Commit English-only code, identifiers, comments, fixtures, UI copy, and documentation.
  `Türkiye` is the intentional display name for country code `TR`. This does not cover
  on-demand generated artifacts (e.g. cover letters), which are written in the job
  posting's own language.
- Never automate a final application submission. The user must review and submit.
- Do not scrape authenticated LinkedIn pages, profiles, connections, or messages. The LinkedIn
  collector reads only LinkedIn's credential-free public job endpoints and never signs in.
- Keep profiles, generated artifacts, browser sessions, OAuth tokens, secrets, and SQLite files
  in the ignored operating-system application-data directory.
- Do not contact external sources before setup is explicitly activated.
- Collectors fail independently and preserve provenance during deduplication.
- Eligibility is a hard gate. Reputation and fit cannot override authorization, sponsorship,
  clearance, or explicit geographic restrictions.
- The `location_authorization` dimension is always computed deterministically from the
  eligibility result and is never scored by a model.
- Country-scoped remote wording is regional, not worldwide. Unknown facts remain unknown.
- Company fit and job fit stay separate; company fit contributes at most 20% of opportunity fit.
- Company search may discover official domains and pages, but search snippets and third-party profiles
  are never evidence. Score only successfully fetched official pages and collected job postings.
- Deduplicate company evidence and describe its coverage; never present source count as model confidence.
  Keep missing company facts unknown and avoid catalog fields that do not improve hiring decisions.
- No-key company research must remain functional when Wikidata or another discovery provider is offline.
- Version scoring behavior so changed rules or prompts requeue stale evaluations exactly once.
- Use official pages, documented APIs, or user-owned messages by default. Document terms,
  attribution, polling limits, and canonical URLs for every source.
- Cover letters must be selective, factual, on demand, and user-reviewed.
- External commands use argument arrays with `shell=False` semantics.

## Verification

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy src/rolebeacon
uv run pytest
uv run python -m build
cmp -s AGENTS.md CLAUDE.md
```
