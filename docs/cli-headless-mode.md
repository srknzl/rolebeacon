# Future: a single headless CLI invocation

Not scheduled - a design note for later, written 2026-08-16 so the idea and the plumbing
it builds on don't need rediscovering.

## What's wanted

One command that, using the same settings JSON the web app already reads (`setup.json`,
`search-preferences.json`, `mobility-profile.json`, `candidate-profile.json` in the app-data
dir - already a defined, versioned schema, see `profile.py`), does a sync and then prints:

- the recommended jobs (eligible, ranked)
- the full job list, sorted by score

optionally starting a local Ollama it manages itself first, behind a flag.

## What already exists

Most of this is already built, just not composed into one call:

- `rolebeacon sync` (`cli.py`) runs one `SyncService.run()` and prints the resulting
  `SyncStatus` as JSON - collection + scoring, headless, already reads the same settings JSON.
- `rolebeacon model start` / `LocalModelService.start_ollama()` (`setup.py:201`) already
  launches a local Ollama via `subprocess.Popen` - the "run its own Ollama" part is done,
  just not wired to a sync invocation.
- `database.list_jobs()` / `dashboard_stats()` already produce the sorted, scored job list the
  web UI renders - the `decision_ready` sort (eligible -> unknown -> ineligible, then score
  desc; see the redesign plan's Phase 4.4) is the right default order for a CLI listing too.

## The actual gap

One new subcommand, e.g. `rolebeacon jobs [--recommended-only] [--start-ollama] [--limit N]`:

1. If `--start-ollama` and `settings.llm_enabled`: call `LocalModelService.start_ollama()`,
   poll `LlmClient.health()` until available or a timeout, same pattern `app.py`'s setup flow
   already uses for the "Start Ollama" button.
2. Run one `SyncService.run()` (reuse `sync`'s existing code path, don't fork it).
3. Call `database.list_jobs(sort="decision_ready", ...)`, print recommended (eligible, above
   threshold) and the full sorted list as JSON (machine-readable, matching `sync`/`status`'s
   existing convention) or a plain table behind a `--format table` flag.

No new schema, no new service class - a new `argparse` subparser in `cli.py` that calls
existing `SyncService`/`LocalModelService`/`Database` methods in sequence. Small.

## Open question worth asking the user before building it

Table output vs. JSON-only: `sync`/`status` are JSON-only today (scriptable, not meant to be
read directly). A `jobs` command is squarely meant to be read directly - worth confirming
whether a human-readable table is in scope or JSON is enough for a first cut.
