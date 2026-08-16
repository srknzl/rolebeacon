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
| `phi4:14b` | n/a - 3 of 4 model-scored rubric cases rejected | n/a | Fast (7.4s median latency) but returns the literal dimension name (`role_domain`, `stack`, ...) as the evidence `requirement` text instead of an actual requirement - fails validation, not a speed problem. |
| `qwen3.6:27b`, default settings | n/a - 0 of 4 calls completed | n/a | Reasons by default. Every call exceeded RoleBeacon's 120s request timeout (median/max ~240s = both retry attempts timing out). |
| `qwen3.6:27b`, `think:false` forced | 4/5 rubric cases passed, both ranking checks passed | grounded, cites specific profile phrases | RoleBeacon now sends `think:false` for this model specifically (`llm.py`). Matches `qwen2.5:14b-instruct-q6_k`'s pass rate at ~24s median latency (vs. ~5.8s) - a real Large-tier option, not a fallback. The one miss: scored 84 against an expected ceiling of 82 on the unknown-eligibility case, a 2-point calibration overshoot, not a validation failure. |

**Prefer instruct-tuned, non-reasoning models over reasoning models of the same or larger size for
this specific task.** A reasoning model spends its budget arguing with the JSON schema instead of
reliably filling it, which is exactly what produced qwen3's generic-evidence and empty-response
rejections in live testing. This is a property of *this task*, not a general claim about model
quality - the same reasoning model may well be the better choice for a different job.

**This does not generalize to "always force thinking off for a Qwen3-family model."** `qwen3:14b`
measurably got *worse* with `think:false` forced (0.32 correlation, collapsed evidence) than with
its own reasoning left on. `qwen3.6:27b` is the opposite: unusable at its own default (every call
timed out) and strong with `think:false` forced. RoleBeacon's request-building code
(`LlmClient._ollama_payload`) scopes the override to `qwen3.6` specifically for exactly this
reason - test each model rather than assuming the family behaves consistently.

## Tiers

Sizes are rough VRAM/unified-memory guidance for a 4-6 bit quantization; check the specific
quant's memory footprint before pulling. Corrected from an earlier draft: a 16 GB card (e.g. an
RTX 5080) is the *Medium* tier below, not Small - it already comfortably runs both models in the
measured table.

### Small - 6-8 GB (entry laptop GPU, base unified-memory machines)

- **`qwen3:8b`** - RoleBeacon's shipped default. Chosen for size, not yet run through
  `rolebeacon evaluate-model` by this project - if you're on this tier, running the command below
  and sharing the result would turn this into a measured entry.
- Worth trying: `qwen2.5:7b-instruct` - same non-reasoning family as the measured 14B winner.
- Worth trying: `qwen3.5:9b-instruct` (non-thinking variant, not `-thinking`) - newer Alibaba
  generation (Feb 2026) in the same size class; Qwen3.5 defaults to thinking mode, so the
  `-instruct` tag specifically must be used to get the direct-answer behavior this task wants.

### Medium - 12-16 GB (RTX 4070 Ti/4080/5080 class, most gaming laptops, base Mac Studio)

- **`qwen2.5:14b-instruct-q6_k`** - measured, recommended (table above).
- `qwen3:14b` - measured, **not recommended** for this task (table above).
- `phi4:14b` (Microsoft) - measured, **not recommended**: fast but structurally unreliable on
  this rubric (table above). Its general reputation for JSON extraction didn't carry over to
  this specific validation-heavy prompt; worth a retry if the evidence-requirement prompt
  wording is loosened, but not swapping in as-is.
- Worth trying: Google's `gemma-4-12b-it` (Jun 2026) - different vendor, non-reasoning-first,
  sized to fit this tier with headroom. Check Ollama library availability at pull time; not yet
  confirmed there as of this research pass.

### Large - 20-24 GB (RTX 3090/4090/5090 class, higher-memory Macs)

- **`qwen3.6:27b`** (Alibaba, Apr 2026) - measured, **recommended for this tier** (table above).
  It does reason by default, and left alone it's unusable (every call exceeded RoleBeacon's 120s
  timeout). RoleBeacon now sends `think:false` for this model automatically, which matches
  `qwen2.5:14b-instruct-q6_k`'s pass rate at ~4x the latency (~24s vs. ~6s median) and ~4x the
  VRAM - pick it over the Medium tier's winner only if you have the memory to spare and want the
  slightly more detailed evidence citations it produced in testing, not because it's a clear
  quality upgrade.
- `mistral-small3.2` (Mistral, 24B dense, ~15 GB) - different vendor, explicitly built around
  instruction-following and function calling rather than reasoning. Not yet tested.
- Also plausible, not yet tested: `qwen2.5:32b-instruct` - same proven family as the current
  winner, one size up.

### Not worth pursuing for this task

Found in this research pass and ruled out, so the reasoning doesn't need re-deriving:

- **GLM-5.2** (744B total / ~40B active MoE) and **Qwen3.5's 397B-A17B flagship** - both need
  far more memory than any tier above even quantized; effectively datacenter hardware.
- **Mistral "Small 4"** (119B total / 6.5B active hybrid) - despite the name, a MoE needing much
  more memory than "Small" implies.
- **DeepSeek-R1 distills** - trained on R1's chain-of-thought traces, i.e. reasoning-first by
  construction; expect the same schema-fighting failure mode measured for qwen3:14b above.
- **Llama 4 Maverick/Scout** - 109B-400B total MoE; not realistically single-GPU.

### Very large - 48 GB+ (multi-GPU rigs, top-end Mac Studio)

Not measured. `qwen2.5:72b-instruct` and `llama3.3:70b-instruct` are the safer non-reasoning
starting points if this tier is available; the finding above still applies; don't assume bigger
or newer beats the measured Medium-tier winner without actually running the eval.

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
