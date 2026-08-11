# Contributing to RoleBeacon

Thank you for helping improve RoleBeacon. Contributions should preserve its local-first,
evidence-based, and human-approved design.

## Development setup

```bash
git clone https://github.com/srknzl/rolebeacon.git
cd rolebeacon
uv sync --extra dev
uv run playwright install chromium
```

Run the complete local check before opening a pull request:

```bash
uv run ruff check .
uv run mypy src/rolebeacon
uv run pytest
uv run python -m build
```

## Pull requests

- Keep changes focused and include tests for behavior changes.
- Update public schemas and migration notes when interfaces change.
- Keep `AGENTS.md` and `CLAUDE.md` byte-for-byte identical.
- Use English in committed code, fixtures, UI copy, and documentation. `Türkiye` is the intended
  country display name for ISO code `TR`.
- Never commit profiles, résumés, job databases, API keys, OAuth tokens, or browser sessions.
- Never add application submission automation.

## New data sources

Use official pages, documented APIs, feeds, or user-owned messages. Document provenance,
attribution, rate limits, polling behavior, terms, and failure isolation in
`docs/data-sources.md`. Authenticated LinkedIn scraping is not accepted.

## Reporting security issues

Follow [SECURITY.md](SECURITY.md). Do not disclose vulnerabilities in public issues.
