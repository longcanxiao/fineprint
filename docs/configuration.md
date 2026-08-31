# Configuration reference

Everything you can set, in one place: `fineprint.yml`, environment
variables, CLI conventions, exit codes, and what lands in `.fineprint/`.

## `fineprint.yml`

Lives at the dbt project root. `fineprint init` writes a commented template.

```yaml
language: en            # caliber card + CLI language: zh | en
metrics:                # dashboard metrics to trace and synthesize cards for
  - key: refund_rate_14d          # your stable identifier (file names, API lookup)
    title: 14-day refund rate     # display title on cards and reports
    target: dm_refund_rate_1d.refund_rate   # model.column to trace
    extra_targets: [dm_refund_rate_1d.channel]  # optional: related columns traced into the same card
    query_filter: "channel = 'live'"            # optional: query-time filter, noted on the card
lexicon: {}             # optional business glossary (term -> meaning); cards may cite it
# internal_packages: [shared_models]
#   optional: extra dbt packages parsed as first-party code. All other
#   third-party packages (Fivetran, dbt_utils, ...) are treated as data-source
#   boundaries: their SQL is not parsed and lineage stops at their tables.
```

Notes:

- `target` is `model.column`; when two packages share a model name, write
  `package.model.column`.
- `language` drives card content **and** the tool's own output — CLI and
  Python API alike. `FINEPRINT_LANG` overrides it; with no signal at all the
  default is English.
- The repository template also contains a `governance:` block — that feature
  (duplicate-metric scanning and arbitration) is scheduled for 2.0 and is not
  part of the PyPI distribution; the block is ignored there.

## Environment variables

LLM credentials are environment-only — they never belong in `fineprint.yml`.
A `.env` file at the project root is honored (only `FINEPRINT_*` / `OPENAI_*`
keys are loaded, and existing environment variables are never overridden).

| Variable | Default | Meaning |
|---|---|---|
| `FINEPRINT_LLM_BASE_URL` | `https://api.openai.com/v1` | any OpenAI-compatible endpoint (self-hosted included) |
| `FINEPRINT_LLM_API_KEY` | — (falls back to `OPENAI_API_KEY`) | required for `synth` only |
| `FINEPRINT_LLM_MODEL` | — | required for `synth`; e.g. `gpt-4.1-mini`, `deepseek-chat` |
| `FINEPRINT_LLM_FAST_MODEL` | = `MODEL` | used for per-model extraction (the cheap, parallel step) |
| `FINEPRINT_LLM_QUALITY_MODEL` | = `MODEL` | used for merging and business narrative |
| `FINEPRINT_LLM_CONCURRENCY` | `8` | max in-flight LLM requests across the whole batch |
| `FINEPRINT_LLM_TIMEOUT` | `180` | per-request timeout, seconds |
| `FINEPRINT_LLM_RETRIES` | `8` | retry budget per request (each retry prints why and how long it waits) |
| `FINEPRINT_LANG` | — | `zh` \| `en`; overrides `fineprint.yml`'s `language` for CLI output |
| `FINEPRINT_GRAPH` | `<project>/.fineprint/graph.json` | override the graph file location |
| `FINEPRINT_DEBUG` | — | set to `1` to get full tracebacks instead of one-line errors |
| `DBT_TARGET_PATH` | — | honored when locating dbt artifacts (same variable dbt uses) |

## CLI conventions

- Every command takes `--project DIR` (default: current directory) and
  `--target-path DIR` (default: `DBT_TARGET_PATH` → `dbt_project.yml`'s
  `target-path` → `target`).
- `synth --only KEY` re-synthesizes one metric and re-publishes a complete
  batch (the other cards are carried over from the active batch).
- `synth --verbose` adds per-step detail; `synth --json` emits one JSON event
  per line for wrappers and CI.
- `trace … --full` annotates the caliber tree with source files, compiled
  line numbers and per-branch source columns.
- `drift --strict` is the CI gate: high-severity drift exits non-zero and
  leaves the baseline and log untouched.
- `report -o FILE` chooses the output path (default
  `.fineprint/caliber_report.html`).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | operation failed: missing inputs, lineage errors, failed batch, high drift under `--strict` — the message on stderr says what to fix |
| `2` | usage error (bad arguments, unknown `--only` key) |
| `3` | the invoked component is not in this distribution (e.g. `govern`, scheduled for 2.0) |
| `130` | interrupted (Ctrl-C) |

Errors print as one actionable line; `FINEPRINT_DEBUG=1` restores the full
traceback.

## The workspace: `.fineprint/`

Everything FinePrint writes stays under the analyzed project:

| Path | Content |
|---|---|
| `.fineprint/graph.json` | the lineage graph (rebuild any time with `fineprint graph`) |
| `.fineprint/store/runs/<id>/` | one JSON per caliber card + `index.json`; batches are atomic |
| `.fineprint/store/active_run` | pointer to the currently published batch |
| `.fineprint/cache/` | content-addressed LLM responses — contains SQL fragments, treat like source code; safe to delete (costs re-synthesis) |
| `.fineprint/caliber_report.html` | default report output |
| drift snapshots + event log | the drift baseline; `--strict` failures do not touch it |

See [privacy.md](privacy.md) for what any of this means for sensitive
codebases, and [python-api.md](python-api.md) for reading the store
programmatically.
