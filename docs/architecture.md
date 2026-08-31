# Architecture

How FinePrint turns dbt artifacts into metric definitions you can trust.

*(Throughout these docs, a metric's **caliber** means its real, executed
definition: the formula, filters, time windows, dedup rules and grain that
actually shape the number — not what the wiki says it should be.)*

## Inputs — and what is never touched

FinePrint reads three files produced by dbt:

| File | Produced by | Used for |
|---|---|---|
| `manifest.json` | `dbt compile` | model list, dependencies, column docs |
| compiled SQL (`target/compiled/…`) | `dbt compile` | the SQL that actually runs |
| `catalog.json` (recommended, optional) | `dbt docs generate` | real column sets of tables |

It never connects to a database and never reads warehouse data. Everything it
claims is derived from these files, which is why every claim can carry a
"file + compiled line number" receipt.

## The pipeline

```text
 dbt artifacts
      │
      ▼
┌────────────────────┐   ┌─────────────────────┐
│ Channel 1           │   │ Channel 2            │
│ deterministic       │   │ LLM reader           │
│                     │   │                      │
│ column-level lineage│   │ reads each model's   │
│ (sqlglot AST)       │   │ SQL, writes a        │
│ + formula composer  │   │ business narrative   │
└─────────┬───────────┘   └──────────┬──────────┘
          │       cross-validation    │
          └────────────┬──────────────┘
                       ▼
        publication state machine
   (VERIFIED / TECHNICAL_ONLY / REVIEW_REQUIRED)
                       ▼
   caliber tree · caliber cards · HTML report · drift log
```

### Channel 1: the deterministic engine

Built on [sqlglot](https://github.com/tobymao/sqlglot). It parses every
model's compiled SQL and walks column lineage across models — through CTEs,
subqueries, `UNION`, `PIVOT`, window functions and joins. For each metric it
extracts:

- **source columns** — the leaf table columns the value comes from;
- **conditions** — every `WHERE` / `HAVING` / `QUALIFY` / `JOIN … ON` clause
  that constrains the metric's rows, each with its model, source file and
  compiled line number;
- **the expression chain** — how the value is transformed layer by layer;
- **structural semantics** — dedup windows, `CASE WHEN` buckets,
  `COALESCE` fallbacks, which date column the stat is attributed to.

A **formula composer** then expands the metric's SQL scope by scope into one
cross-layer formula. When an aggregation happens mid-chain, that step is kept
as a named sub-expression with the model and grain where it is defined —
because inlining across an aggregation boundary would be wrong.

Everything in channel 1 is a plain program: same input, same output, no model
calls. `graph`, `trace` and `drift` run on channel 1 alone.

### Channel 2: the LLM reader

An LLM reads each relevant model's SQL (only the SQL — never channel 1's
results, so the two channels stay independent) and produces a plain-language
narrative: what the metric means, its key rules, its caveats.

The LLM is not trusted. Every SQL quote it makes is checked against the real
SQL; a quote that does not exist is rejected as a hallucination. Every field
name and number in its prose must be traceable to the project's actual SQL
and docs; every business clause must cite a machine-collected evidence ID.

### Cross-validation and the state machine

The two channels are compared item by item: do they agree on source columns,
on filter conditions, on the formula's aggregation structure? The result
decides the card's publication status:

| Status | Meaning |
|---|---|
| `VERIFIED` | machine facts and narrative passed the gate — full card published |
| `TECHNICAL_ONLY` | machine facts are solid; the narrative failed some check and is shown as an unreviewed draft |
| `REVIEW_REQUIRED` | conflicts or ambiguity — no caliber published, only the problem summary and evidence |

For formulas specifically: **when the composer can prove a formula, the
composer's formula is the published one** — the LLM's version is shown only
as a readable paraphrase. Only when the composer explicitly reports "cannot
prove this" (see [known boundaries](known-boundaries.md)) does the
cross-validated LLM formula stand in, and the card says so.

## Outputs

- **Caliber tree** (`fineprint trace`) — terminal view: formula split into
  numerator/denominator, each side's own conditions, shared conditions, and
  the model chain. `--full` adds source files, compiled line numbers and
  per-branch source columns.
- **Caliber cards** (`fineprint synth`) — one JSON per metric plus a batch
  index, published atomically (a batch is either fully live or not at all).
  The card JSON is the integration contract — see
  [python-api.md](python-api.md).
- **HTML report** (`fineprint report`) — a single self-contained file;
  every clause links to its evidence row (original SQL, model, file, line).
- **Drift log** (`fineprint drift`) — snapshots of the graph's semantics are
  compared run over run; changes land as events naming the affected metrics
  and the exact condition that changed. `--strict` turns high-severity drift
  into a non-zero exit code for CI.

## More detail

- Accuracy numbers and how they were measured: [accuracy.md](accuracy.md)
- What leaves your machine and what never does: [privacy.md](privacy.md)
- Every config key and environment variable: [configuration.md](configuration.md)
- What FinePrint knows it cannot do: [known-boundaries.md](known-boundaries.md)
- Which interfaces are stable: [stability.md](stability.md)
