# -*- coding: utf-8 -*-
"""无 catalog 模式:qualify schema 由 yml 声明列 + 编译 SQL 拓扑推断补全。

场景 = 可编译但连不上库的 GitLab 类公开仓库(dbt docs generate 跑不了)。
规则:catalog 永远优先只填缺;推断按依赖拓扑序(上游先推,下游星号可展开);
解析不动的模型缺列,下游按既有边界语义诚实退化。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint.lineage import build_graph  # noqa: E402
from fineprint.project import DbtProject  # noqa: E402
from tests.test_generalize import _cat, _node, make_project  # noqa: E402


def _proj(tmp_path, with_model_catalog=False):
    cats = {"seed.p.raw": _cat("main", "raw", {"a": "INT", "b": "TEXT", "amt": "INT"})}
    if with_model_catalog:
        cats["model.p.stg"] = _cat("main", "stg", {"x": "INT"})
    nodes = {
        "model.p.stg": {**_node("stg", "compiled/stg.sql"),
                        "depends_on": {"nodes": []}},
        "model.p.mart": {**_node("mart", "compiled/mart.sql"),
                         "depends_on": {"nodes": ["model.p.stg"]}},
    }
    return make_project(
        tmp_path, nodes=nodes, catalog_nodes=cats,
        sqls={"compiled/stg.sql": "select a, b, amt from main.raw",
              "compiled/mart.sql": "select * from main.stg"})


class TestSchemaInference:
    def test_missing_models_inferred_topologically(self, tmp_path):
        p = _proj(tmp_path)
        tbl = p.schema[""]["main"]
        assert set(tbl["stg"]) == {"a", "b", "amt"}       # 从 SQL 推断
        assert set(tbl["mart"]) == {"a", "b", "amt"}      # 星号经上游推断展开

    def test_catalog_always_wins(self, tmp_path):
        p = _proj(tmp_path, with_model_catalog=True)
        tbl = p.schema[""]["main"]
        assert set(tbl["stg"]) == {"x"}                   # catalog 声明优先,不被推断覆盖
        assert set(tbl["mart"]) == {"x"}                  # 下游星号按 catalog 列展开

    def test_star_expansion_in_graph(self, tmp_path):
        g = build_graph(_proj(tmp_path))
        assert set(g["models"]["model.p.mart"]["columns"]) == {"a", "b", "amt"}
        ups = g["models"]["model.p.mart"]["columns"]["amt"]["upstreams"]
        assert ups and ups[0]["column"] == "amt"

    def test_catalog_file_optional(self, tmp_path):
        p = _proj(tmp_path)
        (p.target_dir / "catalog.json").unlink()
        p2 = DbtProject(p.project_dir)
        assert p2.catalog_missing
        assert set(p2.schema[""]["main"]["mart"]) == {"a", "b", "amt"}
        g = build_graph(p2)
        assert set(g["models"]["model.p.mart"]["columns"]) == {"a", "b", "amt"}


def _uncompiled_proj(tmp_path):
    """introspective 无编译产物(离线编译被排除的内省模型),但 yml 声明了列;
    down 已编译,join 中裸引用 cnt——归属只能靠 introspective 的声明列。"""
    nodes = {
        "model.p.introspective": {
            **_node("introspective", "compiled/introspective.sql"),
            "depends_on": {"nodes": []},
            "columns": {"user_id": {"name": "user_id"}, "cnt": {"name": "cnt"}}},
        "model.p.down": {**_node("down", "compiled/down.sql"),
                         "depends_on": {"nodes": ["model.p.introspective"]}},
    }
    cats = {"seed.p.other": _cat("main", "other", {"user_id": "INT", "amt": "INT"})}
    return make_project(
        tmp_path, nodes=nodes, catalog_nodes=cats,
        sqls={"compiled/down.sql":
              "select i.user_id, cnt, o.amt from main.introspective i"
              " join main.other o on i.user_id = o.user_id"})


class TestAllowUncompiled:
    def test_uncompiled_raises_without_flag(self, tmp_path):
        p = _uncompiled_proj(tmp_path)
        try:
            _ = p.models
            raise AssertionError("缺编译产物未报错")
        except FileNotFoundError as e:
            assert "introspective" in str(e)

    def test_uncompiled_becomes_boundary_with_flag(self, tmp_path):
        p0 = _uncompiled_proj(tmp_path)
        p = DbtProject(p0.project_dir, allow_uncompiled=True)
        assert "model.p.introspective" not in p.models
        assert "model.p.down" in p.models
        ext = [k for k in p.external_models if k.endswith("introspective")]
        assert ext, "未编译模型应按数据源边界收录"
        # yml 声明列进开放世界 schema(担保回来了)
        assert set(p.schema[""]["main"]["introspective"]) == {"user_id", "cnt"}

    def test_bare_column_attributes_via_declared_cols(self, tmp_path):
        p0 = _uncompiled_proj(tmp_path)
        p = DbtProject(p0.project_dir, allow_uncompiled=True)
        g = build_graph(p)
        cnt = g["models"]["model.p.down"]["columns"]["cnt"]
        assert not cnt.get("error")
        ups = {(u.get("table") or "").split(".")[-1] for u in cnt["upstreams"]}
        assert "introspective" in ups

    def test_qualified_undeclared_col_not_rejected(self, tmp_path):
        """边界表 yml 是部分声明:SQL 显式写 i.extra(未声明列)不得按
        "未知列"废整模型——开放世界不以声明缺席否定 SQL 自身的限定。"""
        p0 = _uncompiled_proj(tmp_path)
        down = p0.project_dir / "compiled" / "down.sql"
        down.write_text(
            "select i.user_id, i.extra, o.amt from main.introspective i"
            " join main.other o on i.user_id = o.user_id")
        p = DbtProject(p0.project_dir, allow_uncompiled=True)
        g = build_graph(p)
        m = g["models"]["model.p.down"]
        assert not m.get("error"), m.get("error")
        ups = {(u.get("table") or "").split(".")[-1]
               for u in m["columns"]["extra"]["upstreams"]}
        assert "introspective" in ups

    def test_closed_world_still_strict(self, tmp_path):
        """封闭世界(全部来源在 catalog)引用不存在列 = 真漂移:
        保持模型级诚实报错,不因开放世界放开而被担保。"""
        nodes = {"model.p.m": {**_node("m", "compiled/m.sql"),
                               "depends_on": {"nodes": []}}}
        cats = {"seed.p.raw": _cat("main", "raw", {"a": "INT", "b": "INT"})}
        p = make_project(
            tmp_path, nodes=nodes, catalog_nodes=cats,
            sqls={"compiled/m.sql": "select r.ghost from main.raw r"})
        g = build_graph(p)
        assert "qualify" in (g["models"]["model.p.m"].get("error") or "")
