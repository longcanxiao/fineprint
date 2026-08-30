# -*- coding: utf-8 -*-
"""dbt unique/relationships 测试 = 基数的声明性证据。

声明由 dbt 按数据定期实测,与 SQL 结构证据(grain/unique_on)走同一覆盖规则:
join 键覆盖伙伴的某个声明唯一键集 → N:1 可证。消费点:
治理 _risk_rows(row_mismatch → duplicate 升档)、output_unique_on(真实表
伙伴不再一票否决唯一性主张)、join_count 质量项缓解标注、卡片 window 事实。
"""
import sys
from pathlib import Path

import sqlglot

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from metriclens.config import MLConfig  # noqa: E402
from metriclens.governance import scan  # noqa: E402
from metriclens.lineage import build_graph, output_unique_on  # noqa: E402
from tests.test_generalize import _cat, _node, make_project  # noqa: E402


def _test_node(name, kwargs, attached=None, deps=None):
    return {"resource_type": "test", "name": f"t_{name}",
            "test_metadata": {"name": name, "kwargs": kwargs},
            "attached_node": attached,
            "depends_on": {"nodes": deps or ([attached] if attached else [])}}


class TestDeclaredTestsParsing:
    def _proj(self, tmp_path, extra_nodes):
        return make_project(
            tmp_path,
            nodes={"model.p.dim": _node("dim", "compiled/dim.sql"), **extra_nodes},
            catalog_nodes={"model.p.dim": _cat("main", "dim", {"id": "INT"}),
                           "seed.p.raw": _cat("main", "raw", {"id": "INT"})},
            sqls={"compiled/dim.sql": "select id from main.raw"})

    def test_unique_and_combination(self, tmp_path):
        p = self._proj(tmp_path, {
            "test.p.u1": _test_node("unique", {"column_name": "ID"},
                                    attached="model.p.dim"),
            "test.p.u2": _test_node("unique_combination_of_columns",
                                    {"combination_of_columns": ["b", "A"]},
                                    attached="model.p.dim")})
        assert p.declared_tests["unique"]["model.p.dim"] == [["id"], ["a", "b"]]
        assert p.declared_unique_rels[".main.dim"] == [["id"], ["a", "b"]]

    def test_relationships_owner_from_deps(self, tmp_path):
        """无 attached_node 时以 to 的 ref 名剔除对端,余者为归属方。"""
        p = self._proj(tmp_path, {
            "test.p.fk": _test_node(
                "relationships",
                {"column_name": "did", "field": "id", "to": "ref('dim')"},
                deps=["model.p.fact", "model.p.dim"])})
        assert p.declared_tests["fk"] == [
            {"uid": "model.p.fact", "column": "did",
             "to_uid": "model.p.dim", "to_column": "id"}]


class TestDeclaredN1Proof:
    """审阅者探针的对照组:SUM over LEFT JOIN,伙伴带 dbt unique 声明 → N:1
    可证 → 行拓扑等同 → 从 row_mismatch(基数未证)升档确定性重复。"""

    def _proj(self, tmp_path, with_test):
        nodes = {"model.p.joined": _node("joined", "compiled/j.sql"),
                 "model.p.plain": _node("plain", "compiled/p.sql"),
                 "seed.p.b": {"resource_type": "seed", "name": "b",
                              "schema": "main", "alias": "b"}}
        if with_test:
            nodes["test.p.ub"] = _test_node("unique", {"column_name": "id"},
                                            attached="seed.p.b")
        return make_project(
            tmp_path, nodes=nodes,
            catalog_nodes={"model.p.joined": _cat("main", "joined", {"revenue": "INT"}),
                           "model.p.plain": _cat("main", "plain", {"revenue": "INT"}),
                           "seed.p.a": _cat("main", "a", {"id": "INT", "amount": "INT"}),
                           "seed.p.b": _cat("main", "b", {"id": "INT"})},
            sqls={"compiled/j.sql": ("select sum(a.amount) as revenue from main.a a "
                                     "left join main.b b on a.id = b.id"),
                  "compiled/p.sql": "select sum(a.amount) as revenue from main.a a"})

    def test_declared_unique_upgrades_to_duplicate(self, tmp_path):
        r = scan(build_graph(self._proj(tmp_path, with_test=True)), MLConfig(metrics=[1]))
        assert r["row_mismatch"] == []
        assert len(r["duplicates"]) == 1

    def test_without_declaration_stays_row_mismatch(self, tmp_path):
        r = scan(build_graph(self._proj(tmp_path, with_test=False)), MLConfig(metrics=[1]))
        assert r["duplicates"] == [] and len(r["row_mismatch"]) == 1

    def test_declaration_on_wrong_key_not_enough(self, tmp_path):
        """声明键未被 join 键覆盖 → 不得援引,保持保守。"""
        p = self._proj(tmp_path, with_test=False)
        p.manifest["nodes"]["test.p.ub"] = _test_node(
            "unique", {"column_name": "other_col"}, attached="seed.p.b")
        r = scan(build_graph(p), MLConfig(metrics=[1]))
        assert r["duplicates"] == [] and len(r["row_mismatch"]) == 1


class TestOutputUniqueOnDeclared:
    SQL = ("select o.id, o.ts from main.raw_o o join main.dim d on o.id = d.id "
           "qualify row_number() over (partition by o.id order by o.ts desc) = 1")

    def test_real_table_join_kills_claim_without_declaration(self):
        ast = sqlglot.parse_one(self.SQL, read="duckdb")
        assert output_unique_on(ast) == []

    def test_declared_partner_keeps_claim(self):
        ast = sqlglot.parse_one(self.SQL, read="duckdb")
        got = output_unique_on(ast, {"main.dim": [["id"]]})
        assert got == ["id"]


class TestJoinCountMitigation:
    def test_n1_proven_annotation(self, tmp_path):
        p = make_project(
            tmp_path,
            nodes={"model.p.cnt": _node("cnt", "compiled/c.sql"),
                   "model.p.dim": _node("dim", "compiled/d.sql"),
                   "test.p.u": _test_node("unique", {"column_name": "id"},
                                          attached="model.p.dim")},
            catalog_nodes={"model.p.cnt": _cat("main", "cnt", {"n": "BIGINT"}),
                           "model.p.dim": _cat("main", "dim", {"id": "INT"}),
                           "seed.p.a": _cat("main", "a", {"did": "INT"}),
                           "seed.p.raw": _cat("main", "raw", {"id": "INT"})},
            sqls={"compiled/c.sql": ("select count(*) as n from main.a a "
                                     "join main.dim d on a.did = d.id"),
                  "compiled/d.sql": "select id from main.raw"})
        r = scan(build_graph(p), MLConfig(metrics=[1]))
        items = [q for q in r["sql_quality"] if q["model"] == "cnt"]
        assert len(items) == 1
        assert items[0].get("n1_proven") is True
        assert "N:1" in items[0]["mitigation"]
