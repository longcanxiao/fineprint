#!/usr/bin/env python3
"""Public Python API (since 0.9) — the minimal programmable surface.

Three entry points, mirroring the CLI's zero-LLM core:

    import fineprint
    fineprint.build_graph("path/to/dbt_project")      # column-level lineage graph
    fineprint.tracing("path/to/dbt_project", "model.column")   # caliber of one column
    fineprint.cards("path/to/dbt_project")            # the published caliber-card batch

Everything else in this package is internal (underscore rules do not apply to
module paths yet — treat anything not exported via ``fineprint.__all__`` as
private). During 0.x these entries are kept as stable as we can make them and
any break is called out in the CHANGELOG; the full library surface (graph
objects, LLM provider protocol, hooks) intentionally waits for real
integrations to pull it into shape.

LLM synthesis (``fineprint synth``) stays CLI-only for now: its contract
(credentials, batching, publishing) is still moving too much to freeze.

中文速记:公开 API 最小面 = build_graph / trace / cards 三入口;卡片批次
JSON 是真正的对外契约(schema_version 冻结),返回对象只是它的类型化镜像。
"""
import json
from pathlib import Path

from fineprint.i18n import t as _t

from fineprint.store import CARD_SCHEMA_VERSION  # noqa: F401  (契约版本,家在 store)

__all__ = ["build_graph", "trace", "cards", "GraphResult", "TraceResult", "Batch",
           "CARD_SCHEMA_VERSION"]


class GraphError(ValueError):
    """Raised when column-lineage extraction fails for some columns/models.

    ``errors`` holds ``(model_uid, column_or_reason)`` pairs. Pass
    ``allow_partial=True`` to write the graph anyway (same as the CLI flag).
    """

    def __init__(self, errors: list):
        self.errors = errors
        preview = ", ".join(f"{m}.{c}" for m, c in errors[:5])
        super().__init__(_t(
            f"列血缘抽取失败 {len(errors)} 处(前 5: {preview});"
            f"allow_partial=True 可强制写图(旧图在此前保持不变)",
            f"column lineage failed at {len(errors)} site(s) (first 5: {preview}); "
            f"pass allow_partial=True to write the graph anyway "
            f"(the previous graph is kept until then)"))


class GraphResult:
    """Summary of a built (and saved) lineage graph — not the graph itself.

    The on-disk graph format is versioned but internal; consume it through
    ``trace``/``cards`` rather than by parsing the file.
    """

    def __init__(self, *, path, models, columns, conditions, semantics,
                 dialect, catalog_missing, unknown_sources, errors):
        self.path = Path(path)
        self.models = models
        self.columns = columns
        self.conditions = conditions
        self.semantics = semantics
        self.dialect = dialect
        #: True when catalog.json was absent (no-catalog mode: schema from yml + inference).
        self.catalog_missing = catalog_missing
        #: Referenced source tables whose column sets are unknown (lineage stops there).
        self.unknown_sources = list(unknown_sources)
        #: Lineage errors kept in the written graph (only with allow_partial=True).
        self.errors = list(errors)

    def __repr__(self):
        return (f"GraphResult(models={self.models}, columns={self.columns}, "
                f"conditions={self.conditions}, semantics={self.semantics}, "
                f"dialect={self.dialect!r}, path={str(self.path)!r})")


def build_graph(project_dir=".", *, target_path=None, allow_partial=False) -> GraphResult:
    """Build the column-level lineage graph from dbt artifacts and save it.

    Reads manifest / catalog / compiled SQL under ``<project_dir>/target``
    (or ``target_path``); never connects to a database. Raises ``GraphError``
    when lineage extraction fails somewhere, unless ``allow_partial=True``
    (then the graph is written and the failures land in ``result.errors``).
    """
    from fineprint.lineage import build_graph as _build
    from fineprint.lineage import graph_errors, save_graph
    from fineprint.project import DbtProject
    project = DbtProject(project_dir, target_dir=target_path)
    graph = _build(project)
    errs = graph_errors(graph)
    if errs and not allow_partial:
        raise GraphError(errs)
    save_graph(project, graph)
    from fineprint.cli import _unknown_sources          # 复用 CLI 的同一判定,不双写
    return GraphResult(
        path=project.graph_path(),
        models=len(graph["models"]),
        columns=sum(len(m["columns"]) for m in graph["models"].values()),
        conditions=sum(len(m["conditions"]) for m in graph["models"].values()),
        semantics=sum(len(m["semantics"]) for m in graph["models"].values()),
        dialect=graph["meta"]["dialect"],
        catalog_missing=project.catalog_missing,
        unknown_sources=_unknown_sources(project, graph),
        errors=errs)


class TraceResult:
    """Deterministic caliber of one column: the S/F/E triple plus the tree view.

    Promised fields — ``target``, ``depth``, ``models_visited``, ``sources``,
    ``conditions``, ``semantics`` — carry exactly what the CLI's flat receipts
    show (dicts with model / kind / sql / line / src_path where applicable).
    ``render()`` returns the CLI text view; ``str()`` is ``render()``.
    """

    def __init__(self, data: dict, _tree_ctx):
        self._data = data
        self._tree_ctx = _tree_ctx      # (project, graph, uid, col) — 树按需渲染
        self.target = data["target"]
        self.depth = data["depth"]
        self.models_visited = data["models_visited"]
        self.sources = data["sources"]
        self.conditions = data["conditions"]
        self.semantics = data["semantics"]

    def to_dict(self) -> dict:
        """The full trace payload (a superset of the promised fields)."""
        return self._data

    def render(self, full: bool = False) -> str:
        """The CLI view: caliber tree when the column is tree-able, flat receipts otherwise."""
        from fineprint.tracing import render
        tree_txt = None
        try:                              # 树是展示增强:任何失败回退平铺,不抛
            from fineprint.tree import caliber_tree, render_tree
            project, graph, uid, col = self._tree_ctx
            tr = caliber_tree(project, graph, uid, col, self._data)
            if tr:
                tree_txt = render_tree(tr, full=full)
        except Exception:
            tree_txt = None
        return render(self._data, tree=tree_txt, full=full)

    def __str__(self):
        return self.render()

    def __repr__(self):
        return (f"TraceResult(target={self.target!r}, depth={self.depth}, "
                f"sources={len(self.sources)}, conditions={len(self.conditions)})")


def _load_graph_or_raise(project) -> dict:
    """加载血缘图;缺席时抛 FileNotFoundError(含 0.8.4 旧工作区搬迁提示)。
    CLI 与库共用这一处文案——统一异常出口负责把它变成人话。"""
    from fineprint.tracing import load_graph
    p = project.graph_path()
    if not p.exists():
        legacy = ""
        if (project.project_dir / ".metriclens" / "graph.json").exists():
            # 0.8.4 工作区改名:整目录搬迁可保留 LLM 缓存/口径批次/漂移历史
            legacy = _t("\n检测到旧工作区 .metriclens/:0.8.4 起统一改名,执行 "
                        "mv .metriclens .fineprint 可原样保留缓存、口径批次与漂移历史",
                        "\nfound legacy .metriclens/ workspace: renamed in 0.8.4 — run "
                        "mv .metriclens .fineprint to keep the cache, card batches "
                        "and drift history intact")
        raise FileNotFoundError(_t(
            f"血缘图不存在({p});请先执行 fineprint graph 或 fineprint.build_graph(){legacy}",
            f"lineage graph not found ({p}); run fineprint graph or "
            f"fineprint.build_graph() first{legacy}"))
    return load_graph(p)


def trace(project_dir=".", target: str = "", *, target_path=None) -> TraceResult:
    """Trace the caliber of ``model.column`` through the saved lineage graph.

    Zero-LLM. Requires ``build_graph`` (or ``fineprint graph``) to have run;
    raises ``FileNotFoundError`` with the fix otherwise, ``KeyError`` when the
    model/column does not exist (with candidate suggestions).
    """
    from fineprint.project import DbtProject
    from fineprint.tracing import resolve_model
    from fineprint.tracing import trace as _trace
    project = DbtProject(project_dir, target_dir=target_path)
    graph = _load_graph_or_raise(project)
    model, col = target.rsplit(".", 1)
    data = _trace(graph, model, col)
    uid = resolve_model(graph, model)
    return TraceResult(data, (project, graph, uid, col))


class Batch:
    """The active published caliber-card batch — a typed mirror of the store JSON.

    The card JSON itself is the contract (``schema_version``, currently 1);
    this object only adds access sugar: ``batch["gmv"]``, iteration, ``len``,
    ``keys()``, ``to_json()``.
    """

    def __init__(self, run_id, at, schema_version, cards: list, index: dict):
        self.run_id = run_id
        self.at = at
        #: Schema version stamped at synth time; None for pre-0.9 batches.
        self.schema_version = schema_version
        self.cards = cards
        self.index = index
        self._by_key = {c.get("metric_key"): c for c in cards}

    def keys(self):
        return list(self._by_key)

    def __getitem__(self, key: str) -> dict:
        return self._by_key[key]

    def __iter__(self):
        return iter(self.cards)

    def __len__(self):
        return len(self.cards)

    def to_json(self) -> str:
        return json.dumps({"index": self.index, "cards": self.cards}, ensure_ascii=False)

    def __repr__(self):
        return (f"Batch(run_id={self.run_id!r}, at={self.at!r}, cards={len(self.cards)}, "
                f"schema_version={self.schema_version})")


def cards(project_dir=".") -> Batch:
    """Load the active published caliber-card batch from ``<project_dir>/.fineprint``.

    Reads the store only — works without dbt artifacts. Raises
    ``FileNotFoundError`` when no batch has been published yet.
    """
    from fineprint.store import CaliberStore
    store = CaliberStore(Path(project_dir) / ".fineprint" / "store")
    d = store.active_dir()
    if d is None:
        raise FileNotFoundError(_t(
            "没有已发布的口径批次;请先执行 fineprint synth",
            "no published caliber batch yet; run fineprint synth first"))
    idx = {}
    idx_f = d / "index.json"
    if idx_f.exists():
        idx = json.loads(idx_f.read_text())
    card_list = [json.loads(f.read_text())
                 for f in sorted(d.glob("*.json")) if f.name != "index.json"]
    return Batch(run_id=idx.get("run_id") or d.name, at=idx.get("at"),
                 schema_version=idx.get("schema_version"), cards=card_list, index=idx)
