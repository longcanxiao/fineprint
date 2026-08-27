# Contributing

Thanks for your interest in MetricLens!

## Setup

```bash
uv venv .venv && uv pip install -p .venv/bin/python -e ".[demo,dev]"
.venv/bin/pytest tests/ -q          # unit tests (no warehouse needed)
.venv/bin/ruff check .
```

To run the full benchmark gates (lineage golden set, trap revelation, governance
evals) you need the reproducible warehouse — `bash jobs/rebuild.sh` builds it from
a fixed seed, then see `docs/DEVELOPMENT.zh.md` for each gate. LLM-dependent steps
(`synth`, `govern`) read credentials from a `.env` in the dbt project root
(template: `.env.example`); everything else runs offline.

## Ground rules

- Every bug fix ships with a regression test that fails before the fix.
- Lineage/governance changes must keep the golden set and trap evals green;
  if a fix legitimately changes expectations, update the golden data in the
  same PR and say why.
- No credentials in code, config files, or fixtures — environment variables only.
- Keep the core package's dependencies to `pyyaml`/`sqlglot`/`requests`;
  anything heavier belongs in the `demo` extra.

## Reporting issues

Include your dbt adapter, sqlglot version, and — if it's a parsing issue — a
minimal compiled SQL snippet that reproduces it. The 14-trap benchmark accepts
new traps: a trap PR needs the data-level validation and a golden entry.
