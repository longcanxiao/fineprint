# FinePrint Stability & Compatibility Policy (draft)

> Status: **draft, pre-1.0.** This document states what we intend to freeze at
> 1.0 and how we behave until then. 中文读者:本页是 1.0 接口冻结宣言的草稿,
> 表格与承诺以英文为准。

## Versioning

FinePrint follows **semantic versioning** from 1.0 onward:

- **major** — breaking changes to any *public surface* listed below.
- **minor** — backward-compatible features; additive fields in stored formats.
- **patch** — fixes and copy changes; no format or flag changes.

Before 1.0 (the 0.x series), minor versions may break anything. We do it
loudly — every break is called out in `CHANGELOG.md` with a migration note —
but we do it without deprecation cycles (the 0.8.4 wholesale rename from
`metriclens` to `fineprint` is the canonical example, and the last of its
kind).

## Public surface (frozen at 1.0)

These are the contracts users may build on. Anything not listed here is
internal.

### 1. CLI

- Command names: `init`, `graph`, `columns`, `trace`, `synth`, `drift`, `report`.
- Documented flags and their meanings (`--project`, `--target-path`,
  `--allow-partial`, `--full`, `--only`, `--strict`, `--dry-run`,
  `--verbose`, `--json`, `-o/--output`, `--force`, `--version`).
- **Exit codes**: `0` success; `1` failure (usage errors, failed batch,
  blocked graph write); `2` argparse usage errors and unknown metric keys;
  `130` interrupted. `drift --strict` exits `1` on high-severity drift.
- `synth --json` event stream: one JSON object per line on stdout with a
  stable `event` field (`batch_start`, `stage`, `retry`, `metric_done`,
  `metric_failed`, `batch_published`). New event types and new fields may be
  added in minors; existing fields keep their meaning.
- Human-readable output (progress lines, error copy) is **not** a contract —
  do not parse it; parse `--json`.

### 2. Configuration: `fineprint.yml`

Top-level keys `language`, `metrics` (`key`, `title`, `target`,
`extra_targets`, `query_filter`), `lexicon`, `internal_packages`, and the
`governance` block. Unknown keys are rejected loudly rather than ignored.
New optional keys may appear in minors; existing keys keep semantics.

### 3. Environment variables

`FINEPRINT_LLM_BASE_URL`, `FINEPRINT_LLM_API_KEY`, `FINEPRINT_LLM_MODEL`,
`FINEPRINT_LLM_FAST_MODEL`, `FINEPRINT_LLM_QUALITY_MODEL`,
`FINEPRINT_LLM_CONCURRENCY`, `FINEPRINT_LLM_TIMEOUT`,
`FINEPRINT_LLM_RETRIES`, `FINEPRINT_LANG`, `FINEPRINT_DEBUG`,
`DBT_TARGET_PATH`. A `.env` file at the analyzed project root is honored for
`FINEPRINT_*`/`OPENAI_*` without overriding the process environment.

### 4. Stored formats

- **Definition card JSON** (`.fineprint/store/runs/<id>/<key>.json`): **this is
  the primary integration contract** — the report, the demo dashboard and the
  public API all consume it. Since 0.9 every card and batch index carries
  `schema_version` (currently 1); a breaking change to documented fields bumps
  it and is announced in the changelog. Documented fields — identity
  (`metric_key`, `title`, `target`, `run_id`,
  `generated_at`, `graph_md5`), the state machine (`publication_status`,
  `status`, `confidence`), `technical_facts` (per-fact `status`, formula
  `authority`/`top`/`defs`/`inline`), `race`, `validation`, `business`,
  `technical`, `evidence` (ids `E*/S*/X*/Q*` with `kind`/`model`/`line`/
  `text`), `governance`, `trace`, `per_hop`. Additive evolution only after
  1.0; consumers must tolerate unknown fields.
- **Batch index** (`index.json`, carries `schema_version` since 0.9) and the
  atomic `active_run` pointer.
- **Lineage graph** (`.fineprint/graph.json`): versioned via
  `fineprint_graph_version` (currently 3). Readers refuse older versions
  with a "rebuild" message instead of guessing; a major graph bump is a
  minor release *if* `fineprint graph` regenerates it losslessly from the
  same artifacts, and is called out in the changelog either way.
- **Drift snapshots & log** under `.fineprint/`: comparisons tolerate older
  snapshot key sets (`sources_full3` → `sources_full` → `sources` fallback
  chain) — old baselines never produce false drift after an upgrade.

### 5. Workspace layout

Everything FinePrint writes lives under `<project>/.fineprint/` (`graph.json`,
`store/`, `cache/`, drift snapshots/log, exported reports by default). The
LLM cache directory contains your compiled SQL — treat it as sensitive; it is
safe to delete (only costs re-synthesis).

### 6. Python API (minimal, since 0.9)

Exactly the names in `fineprint.__all__`: `build_graph(project_dir, *,
target_path=None, allow_partial=False) -> GraphResult`,
`trace(project_dir, "model.column", *, target_path=None) -> TraceResult`,
`cards(project_dir) -> Batch`, plus those three result types and
`fineprint.api.CARD_SCHEMA_VERSION`. The returned objects are typed mirrors
of the stored contracts above (`Batch` of the card JSON; `TraceResult` of the
S/F/E triple). During 0.x these entries are kept as stable as we can make
them and any break is announced in the changelog; the full library surface
(graph objects, LLM provider protocol, hooks) is deliberately deferred until
real integrations pull it into shape. `tests/test_public_api.py` is the
gatekeeper: if it needs editing, the change is breaking.

## Internal (no compatibility promise, at any version)

- Everything importable that is **not** in `fineprint.__all__`: module layout
  (e.g. `fineprint.tracing`, `fineprint.lineage`), function signatures, the
  in-memory graph dict shape.
- Prompt content and the LLM cache key scheme (`PROMPT_VER` bumps invalidate
  caches by design).
- Condition fingerprint internals, scope naming (`alias@n`), progress line
  wording, HTML report markup/CSS.
- The benchmark warehouse, dashboard, and probe scripts in this repo.

## Data egress

Channel 2 sends compiled model SQL and schema.yml descriptions to the LLM
endpoint *you* configure — never warehouse data or credentials, and never
third-party dbt package SQL (data-source boundary). `graph` / `trace` /
`drift` / `report` run fully offline. This behavioral guarantee is part of
the public contract. See SECURITY.md for the full statement.

## Known boundaries (named, not silent)

Every unproven column in our five-corpus benchmark carries a machine reason;
the same honesty applies at runtime: scalar subqueries and def-count
overflows fall back to LLM narration with `authority=llm_fallback`; unparsed
models degrade to data-source boundaries; no-catalog projects with
undeclared source columns get a loud warning naming the tables. If FinePrint
cannot prove something, it says so on the card rather than lowering the bar.

## Roadmap gates for 1.0

1. Public repository (this policy takes effect the day the repo opens).
2. 3–5 external design-partner projects run end-to-end, feedback absorbed.
3. This document promoted from draft to policy; CI runs the test matrix
   (macOS/Linux/Windows × supported Python versions) publicly.
4. Bilingual CLI shipped (0.8.8) and docs reference page for every config
   key and env var.

**Deferred to 2.0:** the governance component (`fineprint govern` —
duplicate-metric fingerprint scan + LLM arbitration) and dbt exposures
integration. They exist in-repo as prototypes and are excluded from
distributions until then; the CLI does not advertise them.
