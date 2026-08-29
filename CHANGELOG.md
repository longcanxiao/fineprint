# Changelog

## 0.7.1 — 2026-08-29

Sixth external review: identity end-to-end in the trust chain, cardinality as
first-class lineage, product-to-graph binding.

- **Cross-validation uses physical three-part identities**: segment-wise
  alignment where a segment unknown on either side is lenient but two declared
  segments must agree — a wrong database or a fabricated schema is kept
  verbatim as a fake identity (extra + missing, capping confidence) instead of
  being tail-folded onto a real source; in-graph model tables remain legal
  per-hop references (filtered, not punished).
- **Formulas must bind to the metric's own value chain**: the formula field is
  screened against a chain-only lexicon (no graph-wide objects), so summing a
  real-but-unrelated column no longer publishes as high; summaries/caveats
  keep the graph-wide vocabulary for comparisons.
- **Row-cardinality is now first-class lineage evidence**: every model records
  its join partners and partner-side equi-keys (`row_risk_joins`); a join is
  provably N:1 — and exempt — when its keys cover the partner's group-by grain
  or window-dedup keys (`unique_on`, both derived deterministically from SQL).
  Fingerprint-equal pairs whose unproven-join leaf row sets differ go to a new
  `row_mismatch` governance tier ("suspected duplicate, cardinality unproven")
  instead of a deterministic duplicate verdict; SUM-over-fanning-LEFT-JOIN vs
  the plain SUM no longer judges as duplicate. `sum(case when … then 1 else
  0 end)` now normalizes into the conditional-count equivalence class.
- **Products bind to their graph**: caliber cards, governance reports and
  drift snapshots stamp `graph_md5`; benchmark acceptance hard-fails on
  stale products, so a rebuilt graph invalidates old green lights.
- **Drift compares logical target identity** (`target_uid`): qualifying a
  target as `package.model.column` for disambiguation is no longer a
  `target_changed`; raw-text comparison remains for legacy baselines.
- **Duplicate-alias scopes are no longer re-merged in traces**: conditions
  and semantics in reused-alias scopes (`s@2`) can't be attributed to a
  specific value path by bare name — they surface in a `scope_ambiguous`
  section (render + card, capping high) instead of silently joining the
  caliber.
- **Docs and canvas stop folding same-name objects**: `column_docs` adds
  `package:model` and `db.schema.identifier` keys (fuller keys queried
  first); the lineage canvas disambiguates source node ids per collision
  (bare → schema.table → three-part).
- Docs corrected: README multi-database support and LLM egress list,
  DEVELOPMENT model names, design-doc as-built deltas.

## 0.7.0 — 2026-08-29

Identity refactor: logical primary keys, physical three-part lookup.

- **unique_id becomes the primary key**: the graph's model dictionary is now
  keyed by dbt `unique_id` (`model.<package>.<name>`) — the logical identity
  that survives environment switches and alias/schema config changes — and
  relation reverse-lookups use full physical three-part names
  (`database.schema.table`, completed from the catalog when SQL writes two
  segments). Short names remain the UI everywhere (config targets, CLI, API,
  cards, reports): unambiguous short names resolve automatically, and a name
  shared by two packages must be qualified as `package.model.column` in
  targets (`pkg:name` in display) — ambiguity errors list the candidates,
  never silently picking one.
- **Unlocked by the new identity model**: multi-database projects (rejected
  since 0.6.0) now load — same `schema.table` in two databases are simply two
  relations; two internal packages defining the same model name (rejected
  since 0.6.1) now coexist. The physical-collision check remains as a defense
  against malformed artifacts (dbt's AmbiguousAlias should make it
  unreachable).
- **Drift**: snapshots add `sources_full3` (`db.schema.table.column`), so
  cross-database repoints are detectable; comparison picks the fullest key
  both sides share (`sources_full3` → `sources_full` → `sources`), and
  display-name fields in snapshots are unchanged — legacy baselines diff
  cleanly with zero upgrade false positives (verified against the demo
  baseline).
- **Governance** fingerprints include the database segment; report pairs and
  sql_quality items print display names; lineage-kinship checks use uids.
- **Graph v3**: readers reject older graphs with a clear "rebuild the graph"
  message — the graph is derived and rebuilds in seconds; caliber cards and
  drift baselines are unaffected.

Also shipped in this release (previously unreleased):

- **Third-party dbt packages become data-source boundaries**: models from
  packages other than the root project are no longer parsed — their SQL,
  schema docs and internal calibers stay outside the trust boundary entirely
  (never sent to the LLM, never in the lexicon, never governance-scanned),
  and lineage stops at their materialized tables exactly like ODS sources.
  Cards tag such sources with the owning package; targeting one of their
  models errors with a pointer to the new top-level `internal_packages`
  list in `metriclens.yml`, which declares owned shared packages that should
  be parsed as first-party code. This also dissolves the cross-package
  model-name collision for genuinely external packages (two internal
  packages sharing a name still fail loudly until the unique_id refactor).
  Projects whose metrics cross package models change caliber shape —
  re-baseline drift snapshots after upgrading. Manifests without
  `metadata.project_name` fall back to `dbt_project.yml`; if neither names
  the root, nothing is folded.
- **SQL quality tier in governance**: row-count aggregations over joins
  (`count(*)`/`sum(1)` with any join in the row set, CTE-transitive) are
  deterministically flagged as a `join_count` semantic point — what such a
  count counts depends on join-key uniqueness and data coverage, neither of
  which the SQL proves, so the metric's meaning silently drifts when data
  changes. The full participant list (tables, join keys, line anchor) lands
  on the lineage trace, the caliber card evidence, drift snapshots, and a new
  `sql_quality` section of the governance report and dashboard console, with
  the standing recommendation to use `count(distinct <pk>)` / `count(<col>)`.
  Window counts and self-evident single-table counts are exempt. Governance
  reports with zero B-tier candidates no longer require LLM credentials.

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
