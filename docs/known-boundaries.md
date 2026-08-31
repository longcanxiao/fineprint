# Known boundaries

What FinePrint knowingly cannot do — named, not silent. When the tool hits
one of these, it says so in its output instead of guessing.

## Where the formula composer refuses

The deterministic engine proves formulas; where it cannot, it refuses with a
machine-readable reason (these are the residual 0.27% on the
[accuracy](accuracy.md) probe):

| Reason | What it means | What happens instead |
|---|---|---|
| scalar subquery | a subquery used as a value inside an expression | the subquery is kept verbatim, not expanded; the formula is marked unproven |
| named-subexpression cap | the expansion would need more intermediate definitions than the display can carry honestly | refused; the lineage and conditions are still full |
| bare-column ownership | a source table declares zero columns (legacy projects), so `SELECT amount FROM a JOIN b` cannot be attributed by structure | the cross-validated LLM reading may stand in, clearly labeled as such |
| multi-target combination | a card combines several `target` columns; how they relate is business intent, not a single SQL fact | each target's formula is proven separately; the combination is narrated, not proven |

When the composer refuses, the card's formula authority switches to
"LLM fallback" and the card says why — it never silently swaps sources.

## Ambiguity the engine reports rather than resolves

- **Reused alias scopes**: when the same alias name is redefined in ways that
  make a condition's home scope ambiguous, the condition is listed under
  "ambiguous attribution" and the card cannot reach the highest confidence.
- **Unknown source columns**: without `catalog.json` and without `columns:`
  declared in your sources yml, lineage stops at that table and `graph`
  prints a loud warning naming every such table (cards over them are
  unlikely to reach `VERIFIED`). Fix: run `dbt docs generate`, or declare
  the columns.
- **Models that cannot be compiled offline** (introspective macros needing a
  live connection): treated as data-source boundaries — lineage stops at
  their materialized tables.

## By design, out of scope

- **Business intent.** FinePrint recovers the definition your code actually
  executes. Whether that matches what the business originally meant is not
  answerable from SQL — that is precisely the conversation the definition card
  is meant to start.
- **Meaning that appears nowhere.** If a rule lives only in someone's head —
  not in SQL, schema.yml docs, or your `fineprint.yml` glossary — the tool
  cannot cite it. Write it down and it becomes citable.
- **Third-party package internals.** Packages like Fivetran transforms or
  `dbt_utils` are treated as data sources: their SQL is not parsed and their
  docs are not trusted as evidence (you can promote a package to first-party
  with `internal_packages` — see [configuration.md](configuration.md)).
- **The BI layer.** FinePrint reads dbt artifacts, not Tableau/Power
  BI/Looker query logs. A filter added inside the dashboard tool is invisible
  to it (BI-layer lineage is on the roadmap).
- **Non-dbt pipelines.** Scheduler-based SQL platforms without dbt-style
  artifacts are a planned adapter, not part of the current release.

## Limits of the LLM narrative

The business-readable half of a card is generated text under hard
constraints: quotes are machine-verified against the real SQL, field names
and numbers must be traceable, clauses must cite machine-collected evidence,
and cards that fail these checks are demoted (`TECHNICAL_ONLY`) or held
(`REVIEW_REQUIRED`) — see [architecture.md](architecture.md).

Those checks bound the facts. They do not prove every sentence of prose is
perfectly phrased. For metrics that matter, have the owner skim the card
before it circulates; the evidence links make that a five-minute job — which
is rather the point.

## Non-goals we get asked about

- FinePrint does not execute SQL, so it cannot tell you a metric's *value*,
  only its *definition*.
- It does not enforce anything by itself: `drift --strict` gives CI a
  non-zero exit code; what to do with it is your pipeline's decision.
- It is not a semantic layer. It documents the warehouse you already have,
  instead of asking you to rebuild it somewhere else first.
