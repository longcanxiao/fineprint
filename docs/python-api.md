# Python API

The minimal public surface, available since 0.9. Built for notebooks, BI
plugins and orchestration tasks that should not have to shell out to a CLI.

```python
import fineprint

fineprint.build_graph("path/to/dbt_project")
result = fineprint.trace("path/to/dbt_project", "dm_refund_rate_1d.refund_rate")
print(result)                                  # the caliber tree, as in the CLI
batch = fineprint.cards("path/to/dbt_project")
batch["refund_rate_14d"]["technical_facts"]    # the card JSON is the contract
```

`import fineprint` is deliberately light: none of the heavy dependencies
(sqlglot, requests) load until you call something.

Exactly three functions are public, mirroring the CLI's zero-LLM core. LLM
synthesis (`fineprint synth`) is CLI-only for now — its contract (credentials,
batching, publishing) is still evolving. Everything not listed on this page
is internal and may change without notice; `fineprint.__all__` is the
authoritative list.

---

## `fineprint.build_graph(project_dir=".", *, target_path=None, allow_partial=False) -> GraphResult`

Builds the column-level lineage graph from the project's dbt artifacts and
saves it to `.fineprint/graph.json`. No LLM, no database.

- `target_path` — where the dbt artifacts live, if not `<project>/target`.
- `allow_partial=False` — when lineage extraction fails somewhere, the call
  raises `fineprint.api.GraphError` (with the failing sites on `.errors`)
  and **keeps the previous graph on disk untouched**. Pass `True` to write
  the graph anyway; the failures are then reported on the result.

`GraphResult` fields:

| Field | Meaning |
|---|---|
| `path` | where the graph was written |
| `models`, `columns`, `conditions`, `semantics` | size of what was extracted |
| `dialect` | SQL dialect detected from the manifest |
| `catalog_missing` | `True` when running without `catalog.json` |
| `unknown_sources` | referenced source tables whose column sets are unknown (lineage stops there — see [known-boundaries.md](known-boundaries.md)) |
| `errors` | lineage failures kept in the graph (only with `allow_partial=True`) |

## `fineprint.trace(project_dir=".", target="model.column", *, target_path=None) -> TraceResult`

The caliber of one column, from the saved graph. No LLM.

Raises `FileNotFoundError` (with the fix) when no graph has been built, and
`KeyError` with candidate suggestions when the model or column does not exist.

`TraceResult`:

- promised data fields — `target`, `depth`, `models_visited`, `sources`,
  `conditions`, `semantics`: exactly what the CLI's receipts show, as lists
  of dicts with `model` / `kind` / `sql` / `line` / `src_path` where
  applicable;
- `render(full=False) -> str` — the CLI view (caliber tree when the column
  is tree-able, flat receipts otherwise); `str(result)` is `render()`;
- `to_dict()` — the full trace payload (a superset of the promised fields).

## `fineprint.cards(project_dir=".") -> Batch`

The currently-published caliber card batch, read from
`<project>/.fineprint/store`. Works from the store alone — dbt artifacts are
not required. Raises `FileNotFoundError` when nothing has been published yet.

`Batch`:

- `run_id`, `at`, `schema_version`, `index`;
- `cards` — list of card dicts; `batch["gmv"]`, `batch.keys()`, iteration
  and `len()` work as expected; `to_json()` serializes the whole batch.

## The card JSON is the real contract

`Batch` and `TraceResult` are thin typed mirrors; the stored JSON is what we
actually promise. Since 0.9 every card and batch index carries
`schema_version` (currently **1**). A breaking change to documented fields
bumps that number and is announced in the changelog. Documented card fields
are listed in [stability.md](stability.md) ("Stored formats").

Consumers must tolerate unknown fields — additions are not breaking.

## Stability promise

During 0.x these three entries are kept as stable as we can make them, and
any break is called out in the [changelog](../CHANGELOG.md).
`tests/test_public_api.py` is the gatekeeper: if that file has to change,
the change is breaking by definition.

The wider library surface — graph objects, extraction internals, an LLM
provider protocol, hooks — is deliberately **not** public yet. We would
rather shape it around real integrations (a BI plugin, a Dagster asset
check) than freeze a guess. If you are building one of those and the three
entries are not enough, open an issue: real callers are exactly what the
next API iteration wants.
