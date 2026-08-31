# Accuracy

What we measured, on what data, and what the numbers do and do not mean.

## Formula coverage on public projects

The deterministic engine (channel 1 — no LLM involved) was run exhaustively
over five public dbt projects: for **every column of every model** it
attempts to compose the full cross-layer formula, and the result counts as
`proven` only when it passes the engine's own round-trip checks (the composed
formula must agree with the independently-extracted lineage on leaf sources
and aggregation structure).

| Project | Nature | Dialect | Models | Columns | proven |
|---|---|---|---:|---:|---:|
| Cal-ITP warehouse | real production (California transit data platform) | BigQuery | 604 | 16,856 | 99.94% |
| Mattermost analytics | real production | Snowflake | 144 | 3,907 | 99.92% |
| Mattermost snowflake-dbt | real production, legacy project | Snowflake | 214 | 5,180 | 98.44% |
| Fivetran ad_reporting | open-source dbt package suite (12 packages) | Postgres | 350 | 6,771 | 100% |
| Snowplow web | open-source dbt package | Snowflake | 52 | 1,785 | 100% |
| **Total** | | | **1,364** | **34,499** | **99.73%** |

Raw probe outputs (per-column status and reasons) live in
`benchmark/reports/` in this repository.

Real production projects bring real complexity — the Cal-ITP warehouse is
full of `UNNEST`, `STRUCT`, date spines and `PIVOT`, and the composer still
produces deterministic expansions, e.g. for a pivoted column:

```text
trips_owl := MIN(CASE WHEN time_of_day = 'owl' THEN n_trips END)
             per [key, service_date, route_id, direction_id]
```

### The 0.27% that is not proven

Every unproven column carries a machine-readable reason — the engine refuses
rather than guesses. The reasons observed across the five projects:

- **scalar subqueries** — a subquery used as a value; its internal logic is
  kept verbatim instead of being expanded into the formula;
- **named-subexpression cap** — the composed formula would need more
  intermediate definitions than the display cap allows;
- **bare-column ownership** — a legacy project declares zero columns for a
  source table, so a bare column reference cannot be attributed to one table
  by structure alone (this is exactly the case the LLM channel exists for).

The full list, with what each one means for you, is in
[known-boundaries.md](known-boundaries.md).

## The trap suite

Formula coverage says the engine can *read* SQL. To check it surfaces the
rules that actually bite analysts, we maintain a hand-built warehouse of
**14 classic metric-definition traps**, each one a pattern we have seen produce wrong
numbers in real teams:

- same metric name, different meaning (same-day refund rate vs 14-day refund rate);
- a status filter hidden in a `JOIN … ON` clause instead of `WHERE`;
- multi-version binlog rows deduplicated by a window function;
- an SCD2 exchange-rate join over validity intervals;
- per-user vs per-order averaging — and ten more.

Each trap is seeded with data that makes the wrong reading produce a visibly
wrong number, and the acceptance test asks: *does the definition card reveal the
trap?* Current result: **14 / 14** (acceptance bar: ≥ 12).

This is our internal regression suite. It keeps the tool from backsliding;
it is not a claim about every project in the wild.

## What these numbers do not mean

- **99.73% is formula provability, not business correctness.** The engine
  proves what the SQL *does*; whether that matches what the business *meant*
  is a question the SQL cannot answer — see
  [known-boundaries.md](known-boundaries.md).
- **LLM narratives are generated text.** They are constrained hard (verified
  quotes, evidence-bound clauses, traceable vocabulary — see
  [architecture.md](architecture.md)), and anything that fails a check is
  demoted or held for review. But machine checks bound the facts, not every
  sentence; for high-stakes metrics, have an owner skim the card before
  circulating it.
