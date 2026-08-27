# Changelog

## 0.6.2 — 2026-08-27

Fifth external review: trust ceiling, join-cardinality modeling, full-chain identity.

- **Trust**: free-text screening now catches single-digit time windows ("限 7
  天内"), prose formulas (formula must express aggregations consistent with the
  channel-1 aggregate signature — fabricated aggregations and aggregation-free
  prose both cap confidence), and fabricated table joins (probe: an invented
  `dim_user` join is caught). The trusted lexicon drops third-party schema
  docs, keeps the user's own metriclens.yml lexicon, and includes all
  graph-known model/column/source names so referencing real objects is never
  flagged; graphs store per-model aggregate signatures (CTE-inner aggregations
  included) to anchor formula checks.
- **Row-count dependencies**: `row_set_tables` follows all join directions
  (LEFT JOIN fan-out changes COUNT(*)), while non-inner join ON conditions are
  no longer marked row-level — the "dependency missing yet condition kept"
  inconsistency is gone.
- **Identity**: cross-validation compares schema-qualified source identities
  (wrong-schema attributions now downgrade with missing+extra recorded; bare
  names only auto-resolve when unambiguous); column docs get schema-qualified
  keys; physical three-part names still resolve.
- **Scopes**: subquery scope names are made unique per AST occurrence
  (alias@n), so legally reused aliases no longer merge scopes and leak
  row-level status.
- **Governance**: AVG ↔ SUM/COUNT expansion pairs go to B-tier arbitration
  instead of a deterministic distinct verdict; pair generation is
  deterministic (sorted) and capped with truncation counts in the report.
- **Config**: NFC+casefold key dedup (case-insensitive filesystems), clear
  errors for non-mapping YAML roots, strict `model.column` target format.
- **Ops**: LLM cache keys include endpoint and max_tokens (same model name on
  a different provider no longer reuses stale responses); the final failed
  retry no longer sleeps; CI builds and typechecks the dashboard; docs/dead
  hints refreshed; as-built demo doc moved to docs/archive/.


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
