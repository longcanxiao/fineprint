# Changelog

## 0.8.4 (2026-08-30)

**Unified naming: everything is `fineprint` now.** `import fineprint` works —
the module directory, CLI command, config file (`fineprint.yml`), workspace
directory (`.fineprint/`), and env-var prefix (`FINEPRINT_LLM_*`,
`FINEPRINT_DB`, `FINEPRINT_GRAPH`) all carry the distribution name. The old
`metriclens` module, CLI alias, `metriclens.yml`, `.metriclens/` and
`METRICLENS_*` names are retired in one clean break (pre-1.0, day-one
package — no compat shims to carry forever). Upgrading replaces the old
module wholesale; rename your project's `metriclens.yml` → `fineprint.yml`,
`.metriclens/` → `.fineprint/`, and `METRICLENS_*` env keys → `FINEPRINT_*`.
The demo warehouse's physical duckdb filenames are data identity, not
branding, and are unchanged.

## 0.8.3 (2026-08-30)

**Caliber tree** (`metriclens/tree.py`): `trace` now leads with a tree view
built from the composer's decomposition. The formula splits at its outermost
arithmetic operator into numerator/denominator branches (transparent
wrappers — ROUND/CAST/parens — are peeled first, and the skeleton like
`ROUND(A / B, 6)` heads the tree); passthrough columns descend to the model
where the caliber is actually defined. Each branch carries its expanded
formula (aliases already resolved to real table names by the composer),
its named subexpressions with defining grain, its **exclusive** conditions,
and a layer-by-layer chain; conditions constraining both sides land in a
shared group. Attribution is row-set-closure based, not value-path based —
a join partner's filters (`status='paid'`, test-account exclusion)
constrain the numerator too, so they group as common instead of being
mis-assigned to one side. Display-only: nothing enters fingerprints or
drift snapshots, and any failure falls back silently to the flat
S/F/E view. Addresses three field reports on the old output: opaque
one-letter aliases, undifferentiated numerator/denominator conditions,
and the four-block reading cost.

## 0.8.2 (2026-08-30)

Docs-only: the PyPI readme's LLM env-var names were wrong
(`METRICLENS_LLM_BASE/KEY` → the real `METRICLENS_LLM_BASE_URL` /
`METRICLENS_LLM_API_KEY`) — following the published 0.8.1 readme would
fail; also notes that graph/trace/drift need no LLM at all. License was
audited and is already fully declared (PEP 639 `License-Expression:
Apache-2.0` + bundled LICENSE; the legacy JSON `license` field is null by
design). `project.urls` stays off until the repository is public — a
commented template is ready in pyproject.

## 0.8.1 (2026-08-30)

Docs-only: the two taglines land in every introduction — *Read the fine
print of your metrics. A decompiler for your dashboards.* (PyPI summary,
PyPI readme, both repo READMEs). No code changes.

## 0.8.0 (2026-08-30)

First PyPI release, published as **`fineprint`** — the fine print of your
metrics. The import name and CLI stay `metriclens` for now (a `fineprint`
CLI alias is installed alongside); a full rename, if any, will come as its
own versioned change.

The public distribution ships the core only: lineage graph, dual-channel
caliber cards, drift detection. The governance component (duplicate scan +
LLM arbitration — now an optional module: `synth` degrades gracefully when
it is absent) and dbt-exposures auto-discovery are excluded from the
published artifacts by `scripts/release_public.sh`; the sdist carries just
`metriclens/` + `README.pypi.md` + `LICENSE`, not the demo warehouse,
dashboard, benchmark, design docs, or tests.

Dual-write race: a deterministic formula composer runs alongside the LLM.
**Race adjudicated (2026-08-30): the composer is now the publishing authority
for formulas; the LLM demotes to explanation/narrative, and serves as the
fallback where the composer cannot prove** (rules where rules can, LLM where
they can't).

- **Authority flip**: cards and the batch index carry
  `technical_facts.formula.authority` (`machine` when the composed formula is
  proven; `llm_fallback` otherwise — multi-target combinations, scalar
  subqueries, and any future unsupported construct). The publication state
  machine is rules-first: proven ⇒ machine facts publish (VERIFIED when the
  LLM narrative also passes all cross-checks, TECHNICAL_ONLY otherwise —
  including `disagree`, which now demotes only the narrative instead of
  blocking the machine facts); unproven ⇒ the LLM formula publishes as
  fallback under its full validation battery (high ⇒ VERIFIED, else
  REVIEW_REQUIRED). `rt_failed` and ambiguous key-filter attribution still
  hard-block. The planned per-disagree adjudication ledger is retired — it
  existed to decide this authority question. Dashboard: the machine-caliber
  section now leads and is labeled the publishing authority; the LLM section
  is labeled explanation/narrative (or "published formula (fallback)" with
  the machine reason when the composer can't prove). Demo distribution is
  unchanged (14 VERIFIED + 1 TECHNICAL_ONLY; 14 machine + 1 llm_fallback).
- **dbt exposures auto-discovery**: exposures (declared downstream consumers —
  dashboards, notebooks, ML feeds, applications) now flow through the whole
  product. The graph carries an `exposures_by_model` reverse map (internal
  models only; source/third-party deps stay out per the data-source-boundary
  convention). Caliber cards gain `consumers` (the exposures on their target
  models, rendered as a 消费方 section with type/URL/owner); drift events are
  targeted — each event lists the affected exposures so an alert names the
  dashboards and owners it hits; governance pairs carry per-side consumer
  lists (a duplicate feeding 3 boards vs. one feeding none makes the
  consolidation direction obvious); and `metriclens init` pre-fills the
  metrics list with commented candidates from exposure-exposed models'
  numeric measure columns (exposures are model-level, so final column
  selection stays human). Demo warehouse declares 3 exposures; parser
  validated on Cal-ITP's 5 real exposures (99 init candidates).
- **dbt tests as declared cardinality evidence**: `unique` /
  `dbt_utils.unique_combination_of_columns` / `relationships` schema tests
  now feed the cardinality proof system as declarative evidence (dbt re-tests
  them against data; same coverage rule as SQL-structural proofs). Join keys
  covering a partner's declared unique key set prove the join N:1 — in the
  governance row-topology risk set (a suspected-duplicate pair whose only
  unproven join is declared-unique upgrades from `row_mismatch` to a full
  duplicate), in `output_unique_on` (a window-dedup uniqueness claim no
  longer dies on a join to a real table whose declared key is covered), and
  for real-table partners (sources/seeds) via a graph-level
  `declared_unique_rels` map. `join_count` SQL-quality items gain an
  `n1_proven` mitigation note when every risk join of the model is proven
  (the residual concern narrows to data coverage). Cards' window fact lists
  declared uniqueness and FK declarations along the visited chain.
- **Join/group context enters channel 1's vision**: traces carry
  `context_tables` — the second class of lineage made visible: row-set /
  join-closure partners of visited models that shape the row set or grain
  without supplying the value (grouping dims, join partners), with column
  inventories for model partners. Cross-validation reclassifies LLM source
  references landing in context from `s_extra_by_llm` (hallucination
  penalty) to `s_context_by_llm` (legitimate context, no penalty), with
  column-existence checks on model contexts so an invented column still
  counts as an extra. Context stays out of fingerprints and drift snapshots;
  cards and the dashboard cross-check line surface it.
- **Formula prompt hardened**: the merge prompt now requires `formula` to be a
  single SQL-parseable aggregate expression (no SELECT/FROM/JOIN, prose goes
  to summary, CJK only inside string literals). Demo rerun: prose verdicts
  4→0, race lands 8 agree + 7 consistent + 0 disagree; one card demonstrates
  `TECHNICAL_ONLY` live (formula machine-agreed, a caveat borrowed the
  neighboring metric's "14"-day number and was caught by the lexicon).
- **Real-world coverage probe** (`benchmark/probe_real_project.py`): runs the
  composer over every model column of any dbt artifacts dir (manifest with
  `compiled_code` + catalog; zero LLM, zero DB). First corpus: Fivetran
  ad_reporting public docs artifacts — 12 production packages, 350 models /
  6771 columns. Probe→fix→re-probe closed at **100.0% proven**
  (98.0% → 99.8% → 100.0%); report persisted under `benchmark/reports/`.
- **Composer coverage fixes from the probe**: heterogeneous UNION branches
  (the cross-platform rollup norm) become a deterministic `union` def
  carrying per-branch labels + expressions instead of unsupported; recursive
  CTE cycles get an explicit scope-level guard, and the depth cap becomes a
  pure backstop (512) after measuring a real 257-level path (codegen'd
  chained CTEs × inline ephemerals × cross-model accumulation).
- **Multi-corpus probes harden channel 1 itself** (Snowplow web, snowflake
  dialect — sessionization/window style): a single unparseable model no
  longer kills the whole graph build — it degrades to a boundary node
  (lineage and the composer both stop at its materialized table, same
  semantics as third-party packages; `metriclens graph` counts it in the
  error gate); model-level qualify falls back to partial qualification so
  one unresolvable column no longer voids a model (per-column strictness
  still decides); `project.schema` backfills catalog-absent source tables
  from first-party manifest yml column declarations (docs-site catalogs
  routinely lack sources; catalog entries always win); bare top-level
  UNION models get their columns via `named_selects` instead of an empty
  projection list.
- **Composer learns standard SQL name-resolution semantics the third
  corpus demanded** (Cal-ITP warehouse, bigquery, 610 hand-written
  models): BigQuery UNNEST lateral sources (element refs compose to the
  underlying array expression — same accounting as sqlglot lineage);
  STRUCT field access (`payload.kind`, incl. the partial-qualify shape
  where the struct name parses as a table qualifier); chained UNIONs
  flattened and aligned **by position** (first branch names, later
  branches bare literals — the dbt_utils date-spine idiom); derived-table
  alias column lists (`as t(c1, c2)`); `t.* EXCEPT(col)` stars don't
  vouch for excluded columns; bare columns in multi-source scopes resolve
  via source claims and `JOIN … USING` leftmost semantics. MAX_DEFS is a
  readability cap, raised 48→200 after real date-spine × struct models
  exceeded it legitimately.
- **PIVOT output columns expand deterministically**: a pivot column is
  rewritten as its measure aggregation with the argument wrapped in
  `CASE WHEN <FOR field> = <value> THEN … END` (`COUNT(*)` becomes
  `COUNT(CASE … THEN 1 END)`), carrying the pivot's implicit grain
  (input columns minus all measure references minus the FOR field) onto
  the def; id columns pass through to the input scope. Output-name ↔
  (measure, value) mapping uses sqlglot's own value-major `columns`
  metadata — no home-grown naming rules. Cal-ITP's 140 pivot columns all
  flip to proven: the corpus closes at **99.9%** (16846/16856; the
  residue is 6 rule-book columns over the defs cap and 4 by-design
  scalar-subquery ambiguities), lifting the three-corpus total to
  **25402/25412 = 99.96% proven**.
- **Fourth corpus: Mattermost data warehouse — no catalog, no DB, offline
  compile** (two real Snowflake projects from `mattermost/mattermost-data-warehouse`,
  compiled against a fake connector so introspective models degrade instead
  of the repo demanding a live warehouse). This corpus forced the open-world
  machinery: `catalog.json` becomes optional (qualify schema = manifest yml
  declarations + topological inference from compiled SQL, catalog always
  wins); `DbtProject(allow_uncompiled=True)` degrades uncompiled first-party
  models to data-source boundaries (their yml columns backfill the open-world
  schema so downstream attribution keeps its claims); qualify's lenient
  retry allows partial qualification for open-world models only (a boundary
  table's partial yml can't veto the SQL's own explicit `t.col` — while
  closed-world models still fail loudly on ghost columns, drift stays
  honest); and bare-column attribution in multi-source scopes gains two
  sound rules grounded in runtime uniqueness (SQL rejects ambiguous bare
  columns, so a query that runs in production has exactly one owner):
  **corpus claims** (any attributed lineage edge anywhere in the project is
  evidence that table has that column) and its dual — when every
  complete-inventory source denies the column, the single remaining
  open-world source must own it. Lateral table functions
  (FLATTEN / SPLIT_TO_TABLE) claim their VALUE element column and never
  deny others. mattermost-analytics: **144 models / 3907 columns / 99.9%
  proven** (residue: 3 scalar subqueries); snowflake-dbt (legacy, 655
  sources with zero column declarations): **214 models / 5180 columns /
  98.4% proven** (residue: 50 bare Salesforce/metering columns whose owner
  is world knowledge — the LLM-fallback lane by design — plus 31 scalar
  subqueries). Probe `--skip-uncompiled` now maps to `allow_uncompiled`
  instead of rewriting the manifest.
- **Dashboard**: the caliber modal gains a
  `publication_status` badge, a "machine caliber" section (per-fact status
  chips, composed top formula, named subexpressions with defining grain /
  join-context / union branches, output grain) alongside the LLM technical
  caliber (labeled as the race-period publishing authority), and the race
  verdict inside the cross-validation section (disagree details inline).
  The governance console gains the `row_mismatch` tier ("suspected
  duplicate, cardinality unproven" with per-side row-set diffs). Batch
  index entries carry `publication_status` and the race verdict per card;
  `.claude/launch.json` gains a `metriclens-api` entry (port 8612).

- **Deterministic technical-formula composer** (`metriclens/render.py`): starting
  from compiled SQL, the target column's expression is expanded scope-by-scope
  (CTEs/subqueries resolved, stars already expanded by qualify) and across model
  boundaries along lineage edges, producing a "top formula + named
  subexpressions" composition. Aggregations and windows are composition
  boundaries: an aggregate definition is never inlined into another aggregate
  (`SUM(SUM(x))`) nor into a join that changes grain — it stays a named
  subexpression annotated with its defining grain. Constructs the composer
  cannot express honestly (divergent UNION branches, self-referencing models,
  scalar subqueries, pathological nesting) degrade to
  `unsupported`/`ambiguous` with machine reasons — the composer's failure mode
  is *miss loudly*, never *fabricate fluently*.
- **Round-trip self-check**: the composed formula must pass the exact same
  chain-lexicon and aggregate-anchor validators applied to the LLM formula,
  and its leaf source set must equal channel-1 lineage (two independent
  implementations cross-proving each other); any failure marks `rt_failed`
  and blocks `VERIFIED`.
- **Per-fact technical block** (`technical_facts` on cards): formula /
  key_filters / sources / window / grain, each with
  `proven|ambiguous|unsupported|unknown` status, machine reasons, and
  evidence ids from the deterministic evidence list.
- **The race** (`race` on cards, aggregated in the batch index): the LLM
  formula is normalized (bare columns, case, rowcount equivalence) and
  compared against the composer's expansion forms — verdicts `agree`,
  `consistent` (no machine contradiction, not structurally matched), `prose`
  (unparseable as SQL), `disagree` (machine-proven contradiction),
  `renderer_unsupported` (coverage datum). Authority does **not** switch in
  0.8: published calibers remain the LLM merge + existing confidence grading;
  the composer's output, disagreements and unsupported rate are the data that
  will adjudicate the switch on real projects.
- **Publication state machine** (`publication_status`): `VERIFIED` (high
  confidence, no machine contradiction), `TECHNICAL_ONLY` (machine formula
  proven, LLM narrative failed cross-validation), `REVIEW_REQUIRED` (formula
  disagreement, ambiguous filter attribution, or round-trip failure).
  First demo race: 15 cards → 3 agree / 6 consistent / 4 prose /
  **2 disagree — exactly the two known-bad LLM cards (gmv, atv)**;
  0 renderer_unsupported; round-trip green 15/15.

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
