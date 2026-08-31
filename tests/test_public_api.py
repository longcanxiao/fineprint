# -*- coding: utf-8 -*-
"""公开 Python API(0.9 最小面)回归:build_graph / trace / cards 三入口。
这套用例就是 API 稳定性的守门员——它跑不通即视为破坏性变更,须公告。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint import api  # noqa: E402
from fineprint.store import CARD_SCHEMA_VERSION, CaliberStore  # noqa: E402
from tests.test_generalize import _cat, _node, make_project  # noqa: E402


def _proj_dir(tmp_path):
    """seed → filtered(where active=1) → metric:两列(count/sum)的最小工程。"""
    p = make_project(
        tmp_path,
        nodes={"model.p.filtered": _node("filtered", "compiled/filtered.sql"),
               "model.p.metric": _node("metric", "compiled/metric.sql")},
        catalog_nodes={
            "seed.p.raw_events": _cat("main", "raw_events",
                                      {"id": "INTEGER", "active": "INTEGER", "amount": "INTEGER"}),
            "model.p.filtered": _cat("main", "filtered",
                                     {"id": "INTEGER", "active": "INTEGER", "amount": "INTEGER"}),
            "model.p.metric": _cat("main", "metric", {"n": "BIGINT", "amt": "HUGEINT"})},
        sqls={"compiled/filtered.sql": "select * from main.raw_events where active = 1",
              "compiled/metric.sql": ("with base as (select * from main.filtered)\n"
                                      "select count(*) as n, sum(amount) as amt from base")})
    return p.project_dir


class TestBuildGraph:
    def test_build_and_summary(self, tmp_path):
        d = _proj_dir(tmp_path)
        r = api.build_graph(d)
        assert r.models == 2 and r.columns >= 4 and r.conditions >= 1
        assert r.dialect == "duckdb" and r.path.exists()
        assert r.errors == [] and r.catalog_missing is False
        assert "models=2" in repr(r)

    def test_lineage_failure_raises_unless_partial(self, tmp_path):
        p = make_project(
            tmp_path,
            nodes={"model.p.bad": _node("bad", "compiled/bad.sql")},
            catalog_nodes={"model.p.bad": _cat("main", "bad", {"x": "INTEGER"})},
            sqls={"compiled/bad.sql": "select ??? garbage"})
        with pytest.raises(api.GraphError) as ei:
            api.build_graph(p.project_dir)
        assert ei.value.errors
        r = api.build_graph(p.project_dir, allow_partial=True)
        assert r.errors and r.path.exists()


class TestTrace:
    def test_trace_fields_and_render(self, tmp_path):
        d = _proj_dir(tmp_path)
        api.build_graph(d)
        r = api.trace(d, "metric.amt")
        assert r.target.endswith("metric.amt") and r.depth >= 1
        assert any(s["column"] == "amount" for s in r.sources)
        assert any("active = 1" in c["sql"] for c in r.conditions)
        txt = str(r)
        assert "metric.amt" in txt
        assert r.to_dict()["expr_chain"]
        assert r.render(full=True)        # full 视图同样可渲染

    def test_missing_graph_raises(self, tmp_path):
        d = _proj_dir(tmp_path)
        with pytest.raises(FileNotFoundError) as ei:
            api.trace(d, "metric.amt")
        assert "fineprint graph" in str(ei.value)


class TestCards:
    def test_batch_access(self, tmp_path):
        ws = tmp_path / ".fineprint" / "store"
        store = CaliberStore(ws)
        rd = store.run_dir("r1")
        card = {"schema_version": CARD_SCHEMA_VERSION, "metric_key": "gmv",
                "title": "GMV", "target": "dm.gmv", "confidence": "high"}
        (rd / "gmv.json").write_text(json.dumps(card))
        (rd / "index.json").write_text(json.dumps(
            {"schema_version": CARD_SCHEMA_VERSION, "run_id": "r1", "at": "t"}))
        store.activate("r1", {"at": "t"})
        b = api.cards(tmp_path)
        assert len(b) == 1 and b.keys() == ["gmv"]
        assert b["gmv"]["title"] == "GMV"
        assert b.schema_version == CARD_SCHEMA_VERSION
        assert json.loads(b.to_json())["cards"][0]["metric_key"] == "gmv"
        assert "run_id='r1'" in repr(b)

    def test_no_batch_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError) as ei:
            api.cards(tmp_path)
        assert "synth" in str(ei.value)


class TestPackageSurface:
    def test_lazy_exports_resolve(self):
        import fineprint
        assert fineprint.trace is api.trace   # 公开函数,勿与内部 fineprint.tracing 模块混淆
        assert fineprint.Batch is api.Batch
        assert "build_graph" in fineprint.__all__ and "cards" in dir(fineprint)
        with pytest.raises(AttributeError):
            fineprint.not_a_thing

    def test_import_stays_light(self):
        # `import fineprint` 不得拖入 sqlglot/requests(notebook 首行体验)
        code = ("import sys, fineprint; "
                "assert 'sqlglot' not in sys.modules, 'sqlglot leaked'; "
                "assert 'requests' not in sys.modules, 'requests leaked'; "
                "print('light')")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=ROOT)
        assert r.returncode == 0 and "light" in r.stdout, r.stderr
