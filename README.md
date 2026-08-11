# RoleBeacon

**Find the roles worth your time.**

RoleBeacon is a local-first job discovery and application assistant. It collects public job
postings, checks location and work-authorization eligibility, scores fit against a user-owned
profile, researches companies with source-backed evidence, and prepares application artifacts
without ever submitting an application.

RoleBeacon works without an LLM. Deterministic rules provide collection, eligibility, ranking,
deduplication, full-text search, and application tracking out of the box. A local or remote
OpenAI-compatible model is an optional quality layer for evidence-rich scoring and writing.

## Highlights

- Incremental catch-up after downtime, with a 30-day first scan and 72-hour overlap thereafter.
- Public ATS and remote-job adapters with per-source health, retry, attribution, and deduplication.
- Versioned candidate, mobility, and search-preference JSON schemas.
- Generated search strategies instead of hardcoded countries or companies.
- Eligibility gates before fit scoring: authorization, sponsorship, relocation, remote scope,
  clearance, blocklists, and explicit exclusions.
- Rules-only scoring by default; optional Ollama or custom OpenAI-compatible endpoints.
- Local SQLite and FTS5 storage in the operating-system application-data directory.
- Built-in HTML/PDF résumé renderer plus a safe external-command extension.
- Provenance-backed company research and a separate company-fit score.
- Selective, factual, on-demand cover letters.
- Human-approved browser preparation that cannot submit an application.

## Screenshots

All screenshots use synthetic candidate, company, and job data.

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="RoleBeacon dashboard with recommended opportunities" width="49%">
  <img src="docs/screenshots/jobs.png" alt="RoleBeacon job discovery and filters" width="49%">
</p>
<p align="center">
  <img src="docs/screenshots/job-detail.png" alt="RoleBeacon evidence-based job detail and application controls" width="49%">
  <img src="docs/screenshots/setup-import.png" alt="RoleBeacon first-run setup wizard" width="49%">
</p>

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Chromium installed through Playwright when PDF résumés or browser preparation are needed

RoleBeacon supports macOS, Linux, and Windows. Docker is intentionally not required.

## Quick start

```bash
git clone https://github.com/srknzl/rolebeacon.git
cd rolebeacon
uv sync --extra dev
uv run playwright install chromium
uv run rolebeacon serve
```

Open `http://127.0.0.1:8787`. The first-run wizard collects your profile and preferences.
No source request or scheduled sync occurs until you review and activate setup.

Run a readiness check at any time:

```bash
uv run rolebeacon doctor
```

## Scoring modes

### Rules only — recommended starting point

Rules-only mode is the default and requires no model, API key, or additional service. It provides
deterministic eligibility and transparent dimension scores. If an LLM later becomes unavailable,
collection still completes and affected jobs remain queued for enhanced scoring.

### Guided Ollama

The setup page can detect an installed Ollama service. Explicit buttons can start an installed
service, pull a selected model, and test the endpoint. It also accepts a LAN Ollama address such
as `http://desktop.local:11434/v1`. The refresh panel reports each collection and scoring step,
whether the configured model is reachable. When an LLM is selected but unavailable, refresh stops before
collection or scoring; fix the endpoint/model or explicitly switch to Rules only, then refresh again.
RoleBeacon never installs Ollama or downloads a model silently.

The default recommendation is `qwen3:8b`. `qwen3:14b` is the higher-quality option for machines
with enough memory, including a 16 GB RTX-class desktop when using an appropriate quantization.

The same actions are available from the CLI:

```bash
uv run rolebeacon model doctor
uv run rolebeacon model start
uv run rolebeacon model pull qwen3:8b
uv run rolebeacon model test --model qwen3:8b
```

### Custom OpenAI-compatible endpoint

Choose **Custom** in setup and provide a base URL, model, and optional API key. This works with
Ollama on another LAN machine, LM Studio, `llama.cpp`'s `llama-server`, and compatible hosted
services. The browser never receives the stored API key.

RoleBeacon does not yet download or manage `llama.cpp` binaries itself. A future managed runtime
can implement the existing provider boundary without changing scoring or profile formats.

## Candidate profile

Setup supports basic form entry, a complete `CandidateProfileV1` JSON document, or a complete
`SetupPayloadV1` document containing the profile, mobility, preferences, selected sources, and
model settings. The wizard uses searchable country pickers backed by the ISO 3166-1 catalog,
then stores the corresponding country codes for matching.
It can also generate a review-only preference draft with an explicitly configured LLM, or copy a
structured planning prompt for any LLM the candidate chooses. Neither path activates collection.
The public schema and CV-conversion prompt are available at:

```text
GET /api/schemas/candidate-profile
```

The conversion prompt instructs any chosen model to use only explicit CV facts. RoleBeacon
validates the result and rejects timeline contradictions before generating application artifacts.

The three stable setup schemas are:

- `CandidateProfileV1`
- `MobilityProfileV1`
- `SearchPreferencesV1`
- `SetupPayloadV1`

Country names are display values; eligibility uses ISO country codes. For example, the interface
displays `Türkiye` while storing `TR`.

## Search strategies and scoring

Setup generates strategies from the user's actual configuration:

- priority companies;
- countries where the candidate already has work authorization;
- relocation targets that may require sponsorship;
- remote work from the candidate's current country;
- an explicit fallback strategy.

The deterministic score totals 100 points:

| Dimension | Points |
| --- | ---: |
| Role and domain match | 25 |
| Relevant skills | 20 |
| Domain experience | 20 |
| Seniority | 10 |
| Location and authorization | 15 |
| Salary and engagement model | 10 |

Remote wording such as “EMEA” or “within your country of employment” is not treated as worldwide.
Unknown eligibility remains visible as a risk. Explicit no-sponsorship, clearance, blocklist, and
geographic restrictions cannot be overridden by company reputation or a high skills score.

## Sources

Built-in source adapters include:

- Arbeitnow
- Jobicy
- Remotive
- Remote OK
- Himalayas
- We Work Remotely
- Greenhouse, Lever, Ashby, SmartRecruiters, and Workday public career endpoints
- optional Adzuna, Jooble, and SerpApi adapters
- optional LinkedIn Job Alert ingestion through a user-owned Gmail label

Only sources selected in setup are enabled. LinkedIn authenticated pages are never scraped.
See [docs/data-sources.md](docs/data-sources.md) before adding or enabling a source.

## Résumés and cover letters

The built-in renderer creates `resume.html`, `resume.json`, and `resume.pdf` from verified profile
facts. Skills may be reordered by relevance, but facts are never rewritten or invented.

An external renderer can be configured as an argument array with these placeholders:

- `{profile}` — verified candidate-profile JSON
- `{jd}` — job-description text file
- `{output}` — required PDF output path

External commands run without a shell. Cover letters are generated only on demand, use verified
candidate and job evidence, and always require review.

## Application safety

RoleBeacon may open supported Greenhouse, Lever, or Ashby forms, fill ordinary contact fields,
and upload a generated résumé. It does not answer salary, legal, demographic, authorization, or
custom questions automatically. It never clicks Submit.

## Local data and privacy

RoleBeacon uses `platformdirs`:

- macOS: `~/Library/Application Support/RoleBeacon`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/RoleBeacon`
- Windows: the current user's local application-data directory

Profiles, SQLite databases, generated applications, browser sessions, OAuth tokens, and secrets
are excluded from Git. API keys are stored in a permission-restricted local secrets file. The web
application binds to `127.0.0.1` by default and rejects cross-origin state-changing requests.

Override storage only when necessary:

```bash
ROLEBEACON_DATA_DIR=/private/path uv run rolebeacon serve
```

## Legacy migration

Import a previous Job Radar installation with a copy-only, idempotent migration:

```bash
uv run rolebeacon migrate --from /path/to/job-radar
```

The importer copies the database and application artifacts when safe, records checksums, and
maps non-secret `JOB_RADAR_*` settings for review. It never moves source data and never imports
Gmail tokens, browser profiles, or LLM API keys. Reauthorize Gmail and browser sessions manually.

## HTTP API

Core endpoints:

- `GET /api/setup/status`
- `GET /api/schemas/candidate-profile`
- `POST /api/setup/profile/validate`
- `POST /api/setup/validate`
- `POST /api/setup/plan`
- `POST /api/setup/model/discover`
- `POST /api/setup/model/test`
- `POST /api/setup/complete`
- `POST /api/sync`
- `GET /api/sync/status`
- `GET /api/model/status`
- `GET /api/jobs`
- `POST /api/jobs/{id}/feedback`
- `POST /api/jobs/{id}/resume`
- `POST /api/jobs/{id}/cover-letter`
- `POST /api/jobs/{id}/prepare-application`
- `GET /api/applications`

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy src/rolebeacon
uv run pytest
uv run python -m build
```

`AGENTS.md` and `CLAUDE.md` are intentionally byte-for-byte identical and must be updated together.

## Phase 2 roadmap

- Gmail OAuth setup and LinkedIn Job Alert ingestion in the web wizard.
- Managed, checksum-verified `llama.cpp` runtime as an Ollama-free optional path.
- User-editable ATS and company registries.
- Deeper provenance-backed company intelligence, hiring signals, and evidence freshness.
- Evidence-based opportunity scoring that combines job fit and company fit without hiding either.
- Selective tailored cover letters with claim-level evidence review.
- Greenhouse, Lever, and Ashby application preparation followed by Workday, Google, and Microsoft.
- Calibration after at least 50 explicit user decisions, measured with precision at 10 and
  eligibility false-positive rate.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Report vulnerabilities using
[SECURITY.md](SECURITY.md), not a public issue.

RoleBeacon is licensed under the [Apache License 2.0](LICENSE).
