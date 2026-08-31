# fineprint quickstart (10 minutes, zero database)

[中文版](README.zh.md)

This directory is a miniature e-commerce warehouse: two raw feeds (orders and
refunds), four dbt models, and two dashboard metrics — **daily GMV** and the
**14-day refund rate**. You are here to answer exactly one question:

> *"What does this refund rate actually count?"*

The dbt build artifacts (`target/`) are bundled, so **no dbt install and no
database** are needed. The first two steps don't even need an LLM.

## 0. Install

Any Python 3.10+ environment:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install fineprint
```

Then either work inside this directory (it runs from wherever you copy it), or
— with no checkout at all — drop the same example anywhere:

```bash
fineprint init --demo && cd fineprint-quickstart
```

## 1. Build the column-level lineage graph (seconds, zero LLM)

```bash
fineprint graph
```

```
graph: 4 models, 15 columns, 7 conditions, 5 semantic points → .fineprint/graph.json  (dialect=duckdb)
```

## 2. Trace the fine print of the refund rate

```bash
fineprint trace dm_refund_rate_1d.refund_rate
```

```
◎ dm.dm_refund_rate_1d.refund_rate
│  output dimensions: stat_date = CAST(paid_at AS DATE)
│  formula: SUM(COALESCE(refund_amount, 0)) / SUM(raw_orders.amount)
│
├─ numerator  SUM(COALESCE(refund_amount, 0))
│  ├─ where refund_amount = SUM(raw_refunds.refund_amount) aggregated by order_id (via join)
│  ├─ caliber: r.refunded_at <= o.paid_at + INTERVAL '14' DAY   (dm_refund_rate_1d.refund_14d)
│  ├─ caliber: rn = 1   (stg_refunds)
│  └─ chain: stg_refunds → this layer
│
├─ denominator  SUM(raw_orders.amount)
│  └─ chain: stg_orders → this layer
│
└─ caliber shared by both sides
   ├─ status = 'paid'   (dm_refund_rate_1d.paid_orders)
   └─ is_test = 0   (stg_orders)
```

The formula splits at its outermost operation into numerator and denominator
branches, and every clause files where it belongs: the 14-day window and the
dedup rule are **numerator-only**; "paid orders only" and "test accounts
excluded" constrain the row set both sides share, so they land in the
**shared** group (the numerator reaches orders through a join and is filtered
by them too — exactly what a value-path-only reading would misplace). Add
`--full` to pin a source file and compiled line-number anchor on every clause.

These clauses are not decoration. The bundled data contains one 200-yuan
refund issued 19 days after payment: under the 14-day caliber, August 1st's
refund rate is **30%** — under a 30-day caliber it would be **80%**.

## 3. Configure an LLM (needed for the next step)

> No LLM key? A pre-built card batch ships with this example
> (`.fineprint/store/`) — skip to step 5 and export the report; come back
> any time.

```bash
cp .env.example .env    # fill in your key (any OpenAI-style endpoint works)
```

## 4. Dual-channel caliber synthesis

```bash
fineprint synth
```

```
✓ daily_gmv         conf=high  F-cover 100%  S miss/extra 0/0  suspect 0  unproven clauses 0  lexicon misses 0  race agree→VERIFIED
✓ refund_rate_14d   conf=high  F-cover 100%  S miss/extra 0/0  suspect 0  unproven clauses 0  lexicon misses 0  race agree→VERIFIED
dual-write race: agree=2
publication: VERIFIED=2
```

Two channels converge here: a **deterministic formula composer** (the
publishing authority) unfolds a machine-provable formula from the compiled
SQL, while the **LLM** reads the same chain and writes the business
narrative. A card is stamped `VERIFIED` only when the two agree (`agree`) or
are structurally equivalent with nothing contradicted (`consistent`), and
every sentence of the narrative traces back to the lineage vocabulary. (LLM
wording drifts across runs, so the `agree`/`consistent` split varies batch to
batch — the composer never drifts; that is why formula authority belongs to
the machine.)

## 5. Export the caliber-card report

```bash
fineprint report
open .fineprint/caliber_report.html     # Windows: start, Linux: xdg-open
```

## 6. The drift experiment: someone quietly turns 14 days into 30

Establish the baseline, then tamper:

```bash
fineprint drift        # first run: baseline established
```

Open `target/compiled/fineprint_quickstart/models/dm/dm_refund_rate_1d.sql`,
change `INTERVAL 14 DAY` to `INTERVAL 30 DAY`, then:

```bash
fineprint graph && fineprint drift
```

```
caliber drift check: 2 events

  ⚠ [high  ] refund_rate_14d            condition_removed  r.refunded_at <= o.paid_at + INTERVAL '14' DAY
  ⚠ [high  ] refund_rate_14d            condition_added    r.refunded_at <= o.paid_at + INTERVAL '30' DAY
```

The change lands on the affected metric and the exact condition — not "some
file changed", but "the 14-day refund rate's time window went from 14 to 30
days". Revert the SQL when you're done.

> In a real project you edit `models/*.sql` and `dbt compile` refreshes the
> artifacts fineprint reads; here you edit the compiled artifact directly so
> the experiment works without installing dbt.

## 7. (Optional) Rebuild the warehouse from scratch

To see the full loop (edit model SQL → dbt compile → caliber changes),
install dbt:

```bash
pip install dbt-duckdb
dbt seed --profiles-dir . && dbt run --profiles-dir . && dbt docs generate --profiles-dir .
```

---

**Notes**

- Everything lands in `.fineprint/`: the lineage graph, card JSON batches
  (`store/runs/<batch>/`), the HTML report, drift snapshots and event log.
- This example ships with `language: en` (the pre-built batch is in English).
  For the Chinese experience set `language: zh` in `fineprint.yml` — CLI, tree
  and cards all follow — and re-run `fineprint synth` for Chinese cards; see
  [README.zh.md](README.zh.md).
- The command is `fineprint` (since 0.8.4; the old `metriclens` command and
  import name are retired).
- The PyPI distribution is the core edition: duplicate-metric governance
  (`fineprint govern`) and dbt exposures integration are scheduled for 2.0 and
  are not part of the package or its CLI.
