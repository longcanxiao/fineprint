# FinePrint

**Read the fine print of your metrics — a decompiler for your dashboards.**

FinePrint recovers, from your dbt project's compiled artifacts, the caliber a
metric **actually executes** — its true definition, in the small print of the SQL.

It can tell you:

- how a metric's formula is computed;
- where the numerator and the denominator each come from;
- which filters, time windows and dedup rules shape the result;
- which upstream columns the metric depends on;
- whether a SQL change silently changed the metric's meaning.

FinePrint never connects to your database and never reads your business data.

## Example

The dashboard says the refund rate is 30%. Finance computes 80%.

Walking the SQL chain reveals what the dashboard actually counts:

- only refunds issued within 14 days of payment;
- only the latest record of each refund;
- test orders excluded;
- paid orders only;
- attributed to the payment date, not the refund date.

FinePrint unfolds those conditions, buried across multiple layers of SQL, directly:

```text
$ fineprint trace --project . dm_refund_rate_1d.refund_rate

◎ dm.dm_refund_rate_1d.refund_rate
│  formula: SUM(refund_amount) / SUM(amount)
│
├─ numerator
│  ├─ refunded_at <= paid_at + INTERVAL '14' DAY
│  └─ rn = 1
│
├─ denominator
│  └─ from stg_orders
│
└─ shared by both sides
   ├─ status = 'paid'
   └─ is_test = 0
```

This caliber tree is derived from the SQL by a deterministic program — no LLM involved.

## Core capabilities

### Metric caliber tracing

Column-level lineage built on `sqlglot`, unfolding across models:

- the formula;
- numerator / denominator;
- filter conditions;
- time windows;
- column dependencies.

```bash
fineprint graph --project .
fineprint trace --project . model.column
```

### Metric caliber cards

FinePrint runs two independent channels:

- a **deterministic engine** deriving technical facts from the SQL AST;
- an **LLM reader** producing a business-readable narrative.

The two are cross-validated before a traceable caliber card is published.

```bash
fineprint synth --project .
fineprint report --project .
```

`synth` sends the relevant compiled SQL and column documentation to the LLM
endpoint you configure; database credentials and warehouse data are never sent.
Every other command runs entirely locally.

### Caliber drift detection

Detects:

- formula changes;
- filter changes;
- time-window changes;
- source-column and dependency changes;
- the downstream metrics affected.

```bash
fineprint drift --project .
```

`--strict` makes it a CI gate.

## Accuracy

The deterministic engine has been probed exhaustively on:

- 5 public dbt projects;
- 3 SQL dialects;
- 1,364 models;
- 34,499 columns.

Provable cross-layer formula coverage: **99.73%**. (Coverage measures formula
provability — it is not the same thing as business-caliber accuracy.)

A hand-built suite of 14 classic caliber traps serves as regression:
currently **14 / 14**.

## Install

```bash
pip install fineprint
```

## Quick start

```bash
cd your-dbt-project

dbt compile
dbt docs generate

fineprint init --project .
fineprint graph --project .
fineprint trace --project . model.column
```

To generate caliber cards:

```bash
export FINEPRINT_LLM_BASE_URL=https://api.openai.com/v1
export FINEPRINT_LLM_API_KEY=sk-...
export FINEPRINT_LLM_MODEL=gpt-4.1-mini

fineprint synth --project .
fineprint report --project .
```

After changing SQL:

```bash
dbt compile
fineprint graph --project .
fineprint drift --project .
```

`graph`, `trace` and `drift` never call an LLM.

## Python API

Since 0.9, a minimal public surface for notebooks, BI plugins and orchestration:

```python
import fineprint

fineprint.build_graph("path/to/dbt_project")
print(fineprint.trace("path/to/dbt_project", "dm_refund_rate_1d.refund_rate"))
batch = fineprint.cards("path/to/dbt_project")   # the card JSON is the contract (schema_version frozen)
```

## Current boundaries

What FinePrint recovers is:

> **the caliber your code actually executes.**

It cannot judge on its own whether the SQL matches the business's original intent.

The current stable release targets dbt projects.

License: **Apache-2.0**
