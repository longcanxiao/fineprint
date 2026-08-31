# Privacy and data boundaries

Exactly what FinePrint reads, what it sends where, and what never leaves
your machine.

## What FinePrint reads

Only dbt build artifacts, from your local disk:

- `target/manifest.json` — model metadata, dependencies, column documentation;
- `target/compiled/**.sql` — the compiled SQL of your models;
- `target/catalog.json` (optional) — table/column listings;
- `fineprint.yml` — your metric list and glossary;
- `.env` at the project root — only keys starting with `FINEPRINT_` or
  `OPENAI_` are loaded, nothing else.

It never connects to a database. It has no way to read rows from your
warehouse: there is no connection string anywhere in the tool.

## Commands that are fully local

`graph`, `trace`, `drift`, `report`, `init`, `--version` make **zero network
calls**. You can run them on an air-gapped machine.

## The one command that talks to a network: `synth`

`fineprint synth` generates the business-readable half of caliber cards by
calling the LLM endpoint **you** configure (`FINEPRINT_LLM_BASE_URL` — any
OpenAI-compatible API, including self-hosted ones).

Sent to that endpoint, per metric:

- the compiled SQL of the models on that metric's lineage path;
- column documentation from your schema.yml files (via the manifest);
- your `fineprint.yml` glossary entries and metric titles;
- the deterministic evidence list (conditions/expressions extracted from
  that same SQL).

Never sent, to anywhere:

- database credentials (FinePrint never has them);
- warehouse data (FinePrint never has it);
- your LLM API key is sent only to the endpoint you configured, as the
  `Authorization` header — never anywhere else;
- anything from models outside the traced metric's lineage.

If your SQL itself is sensitive, point `FINEPRINT_LLM_BASE_URL` at a
self-hosted or in-VPC endpoint that meets your organization's requirements.
The tool does not care which model serves the API.

## What lands on disk

Everything FinePrint writes stays inside the analyzed project, under
`.fineprint/`:

- `graph.json` — the lineage graph (derived from your SQL);
- `store/` — caliber card batches (JSON);
- `cache/` — LLM responses, keyed by content. **This cache contains
  fragments of your compiled SQL.** Treat the whole `.fineprint/` directory
  with the same care as your source code (it is safe to delete; deleting it
  only costs re-synthesis).
- drift snapshots and the drift event log;
- `caliber_report.html` when you export a report.

`.fineprint/` and `.env` belong in your `.gitignore` unless your team decides
otherwise (the cards themselves are often worth committing — that is your
call).

## Telemetry

There is none. FinePrint phones home to nobody: no usage analytics, no
version pings, no crash reporting. The only outbound traffic is the `synth`
call to the endpoint you configured yourself.
