# Security Policy

## Reporting

Please report suspected vulnerabilities privately via GitHub Security Advisories
(or a private issue if advisories are unavailable). Do not open public issues
for unpatched vulnerabilities.

## Scope notes

- MetricLens never connects to your warehouse; it reads dbt artifacts
  (`manifest.json`, `catalog.json`, compiled SQL) from the local filesystem.
- The synthesis and arbitration commands send compiled model SQL, schema.yml
  column descriptions, and the `metriclens.yml` lexicon to the LLM endpoint
  you configure. Treat `.metriclens/cache/` as containing your SQL.
- LLM credentials are read from environment variables / a project `.env` only;
  they never enter configuration files or generated artifacts.
- SQL comments and third-party dbt package SQL are untrusted LLM input.
  Verbatim-quote verification and evidence binding constrain what a
  prompt-injected response can put into a published card, but cards generated
  from untrusted model code deserve human review before publishing.
