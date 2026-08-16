# Headless job discovery command

Status: implemented as `rolebeacon jobs`.

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

## Implemented interface

```text
rolebeacon jobs [--from-json PATH] [--no-sync] [--start-ollama] [--output-dir PATH]
```

The command refreshes by default through `SyncService.run()` and uses `--no-sync` for a strictly local
export. `--start-ollama` checks the configured model first, starts only an installed Ollama when needed,
and waits up to 30 seconds; it never installs a runtime or downloads a model.

`--from-json` accepts one complete `SetupPayloadV1` containing candidate, mobility, preferences, selected
sources, model settings, and activation. It is validated and persisted through the same `SetupService` as
the web wizard before export. A refreshing run requires explicit activation in that document; a local-only
run does not.

Each run atomically creates a timestamped directory containing recommended and all-jobs exports in
versioned JSON and Markdown. The complete export has no row cap and preserves every source association.
The recommendation subset intentionally matches the web dashboard: raw job-fit score at least 65 and
eligibility not `ineligible`. Output files record whether refresh was requested and performed, plus its
final status, so a fatal refresh can still produce an auditable stale-data export before exiting 1.

Successful and local-only exports return 0. A refresh completed with independent source failures returns 0
with a warning. Invalid flag combinations return 2 without creating an export.

The command remains discovery-only. Feedback, artifact generation, and browser preparation stay in the web
workflow and their existing dedicated interfaces.
