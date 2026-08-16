# Local model guide: what to run, by hardware tier

RoleBeacon's rules-only mode is a complete product with no model dependency (see CLAUDE.md).
This guide is only for people who choose to enable an LLM endpoint for written evidence, company
assessments, and cover letters. It is intentionally honest about which entries are *measured* on
this project's own rubric versus *recommended but not yet run* - do not read an unmeasured entry
as equivalent evidence to a measured one.

## The one finding that shapes every tier below

RoleBeacon's scoring prompt is a shallow structured-output task: fill a fixed JSON schema against
a rubric, don't invent facts. It is not a task that benefits from long chain-of-thought. Measured
on a 25-job stratified sample plus a 5-case rubric fixture (`rolebeacon evaluate-model`):

| Model | Rank correlation vs. rules | Evidence grounding | Notes |
|---|---|---|---|
| `qwen2.5:14b-instruct-q6_k` | 0.87 | 97% | Instruct-only, no reasoning mode. **Recommended.** |
| `qwen3:14b`, reasoning forced off | 0.32 | lower, 21 generic-label violations | Frequently collapsed relevant jobs toward zero. |
| `qwen3:14b`, reasoning on | better than above, still trailing | - | 2-4x slower; still lost to qwen2.5 on the same jobs. |

**Prefer instruct-tuned, non-reasoning models over reasoning models of the same or larger size for
this specific task.** A reasoning model spends its budget arguing with the JSON schema instead of
reliably filling it, which is exactly what produced qwen3's generic-evidence and empty-response
rejections in live testing. This is a property of *this task*, not a general claim about model
quality - the same reasoning model may well be the better choice for a different job.

## Tiers

Sizes are rough VRAM/unified-memory guidance for a 4-6 bit quantization; check the specific
quant's memory footprint before pulling.

### Small - 8-16 GB (base Apple Silicon, one mid-range consumer GPU)

- **`qwen3:8b`** - RoleBeacon's shipped default. Chosen for size, not yet run through
  `rolebeacon evaluate-model` by this project - if you're on this tier, running the command below
  and sharing the result would turn this into a measured entry.
- Worth trying: `qwen2.5:7b-instruct` - same non-reasoning family as the measured 14B winner.

### Medium - 24-32 GB (RTX 4090/5090 class, higher-memory Macs)

- **`qwen2.5:14b-instruct-q6_k`** - measured, recommended (table above).
- `qwen3:14b` - measured, **not recommended** for this task (table above).
- Worth trying: `qwen2.5:32b-instruct` - same proven family, one size up, not yet measured here.

### Large - 48 GB+ (multi-GPU rigs, Mac Studio class)

Nothing in this tier has been run against RoleBeacon's rubric yet. Reasonable non-reasoning
candidates to start from: `qwen2.5:72b-instruct`, `llama3.3:70b-instruct`. Given the finding
above, a reasoning-capable large model is not automatically the better pick - test before trusting
size as a proxy for quality on this task.

## Test your own candidate

Any Ollama or OpenAI-compatible model can be measured the same way the table above was produced:

```bash
uv run rolebeacon evaluate-model \
  --base-url http://your-ollama-host:11434/v1 \
  --model your-candidate \
  --runs 2 \
  --output candidate-eval.json
```

This runs the same 5-case rubric fixture used for the table above: score bands, evidence/gap
requirements, ranking checks, eligibility handling, and schema consistency, plus median/maximum
latency. It's read-only against your live database - nothing here gets written back or requeued
until you actually switch `llm.model` in Settings.
