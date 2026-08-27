# Changelog

## 0.6.1 — 2026-08-27

Fourth external review: scope correctness, identity, and trust hardening.

- **Scopes**: inline subqueries get their own scope — conditions inside a
  `LEFT JOIN (SELECT … WHERE …)` no longer pollute the row set of `COUNT(*)`
  metrics; `FROM (subquery)` conditions are correctly row-level; output grain
  resolves through inline aggregate subqueries.
- **Identity**: cross-package model name collisions fail loudly (dbt
  `unique_id` support on the roadmap); sources are keyed by
  `schema.identifier`, so multi-schema projects with same-named sources
  (`erp.orders` + `crm.orders`) load correctly; trace sources carry `schema`,
  governance fingerprints use full names (no cross-schema duplicate false
  positives) and drift snapshots add `sources_full` (legacy snapshots compare
  bare names — no upgrade false positives, cross-schema repoints now detected).
- **Trust**: free-text card fields (formula, summary, definition, caveats,
  key filters) are screened against a channel-1 lexicon — unsourced field
  references or metric numbers cap confidence below `high`; SECURITY.md
  describes the exact constraint layers instead of overclaiming.
- **Governance**: the rowcount equivalence class (`count(*)` ≡ `count(1)` ≡
  `sum(1)`) is normalized before the aggregate-signature verdict; report
  counts A-tier verdicts of both kinds (`a_tier_dup` + `a_tier_agg_distinct`),
  and the dashboard labels verdict provenance (A direct vs B LLM) and shows
  skipped-over-cap candidates.
- **Ops**: `metriclens graph` validates before writing — a failed run no
  longer clobbers the last good graph; the API server reloads the graph on
  file mtime change (no restart after rebuild); `metriclens.yml` gets full
  type validation (non-negative `max_llm_pairs`, list/str contracts).

## 0.6.0 — 2026-08-27

Hardening release driven by an external security/generalization review.

- **Correctness**: sqlglot 30 renamed `Select` arg `from` → `from_`; the row-set
  closure read the dead key, so FROM-linked CTE conditions lost their row-level
  flag. `COUNT(*)`-style projections (no column reference) now fall back to
  table-level row-set upstreams instead of silently dropping every upstream
  filter. `metriclens graph` exits non-zero on column lineage errors
  (`--allow-partial` to override).
- **Safety**: metric keys are validated (charset, uniqueness, reserved names);
  batch publish asserts card set == configured metric set; `schema.table`
  relation collisions (multi-database projects) fail loudly instead of folding.
- **Drift**: `--strict` is a real gate — high drift exits 1 with baseline and
  event log untouched; snapshots carry `query_filter` and diff `target`;
  microsecond snapshot names; locked log writes.
- **Governance**: aggregate signatures (fn + DISTINCT) and output grain
  (resolved through the FROM chain) split "same metric family, different
  grain" and "deterministically distinct" tiers away from duplicates;
  B-tier LLM arbitration capped by `governance.max_llm_pairs`.
- **LLM client**: non-retryable 4xx fail fast; `Retry-After` honored;
  exponential backoff with jitter; global concurrency cap
  (`METRICLENS_LLM_CONCURRENCY`).
- **Packaging**: core deps reduced to pyyaml/sqlglot/requests (demo stack moved
  to the `demo` extra), `requires-python >= 3.10`, version ranges, README in
  wheel metadata, CI (3.10/3.13), CONTRIBUTING/SECURITY docs.

## 0.5.0 — 2026-08-26

First generalized release: `metriclens` package + CLI driven entirely by dbt
artifacts (12 adapters), bilingual prompts, per-project `.metriclens/`
workspace, atomic caliber batches, drift snapshots, governance scan +
arbitration, HTML report. Validated on the 14-trap benchmark warehouse and
jaffle_shop.
