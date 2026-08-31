# -*- coding: utf-8 -*-
"""第六轮外部复审探针固化:互验三段身份、公式链内词表、行集拓扑判重、
漂移逻辑目标、文档全名键、别名复用归因、条件计数等价类、产物-图绑定。"""
import sys
from pathlib import Path

import sqlglot

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint.config import MLConfig  # noqa: E402
from fineprint.governance import scan  # noqa: E402
from fineprint.lineage import agg_one, build_graph  # noqa: E402
from fineprint.tracing import trace  # noqa: E402

from tests.test_generalize import _cat, _node, make_project  # noqa: E402

_CLS = {"f1_fps": {}, "f2_fps": set(), "out_of_scope": [], "unparsed": [],
        "suspect": [], "quote_fail": 0}


class TestCrossValidateIdentity:
    """互验必须用物理三段身份:db 错配、伪造 schema 都不得 high。"""

    _t = {"sources": [{"table": "orders", "schema": "erp", "database": "db1",
                       "column": "amount"}],
          "conditions": [], "semantics": []}
    _legal = {"orders", "erp.orders", "db1.erp.orders"}

    def _hops(self, table):
        return {"m": {"columns": {"x": {"source_columns": [
            {"table": table, "column": "amount"}]}}}}

    def _run(self, table):
        from fineprint.synth import cross_validate
        return cross_validate(dict(self._t), self._hops(table), dict(_CLS), set(self._legal))

    def test_exact_three_part_matches(self):
        v = self._run("db1.erp.orders")
        assert v["confidence"] == "high" and not v["s_missing_by_llm"]

    def test_two_part_completes_database(self):
        assert self._run("erp.orders")["confidence"] == "high"

    def test_wrong_database_downgrades(self):
        v = self._run("db2.erp.orders")
        assert v["confidence"] == "low"
        assert v["s_missing_by_llm"] == ["db1.erp.orders.amount"]
        assert v["s_extra_by_llm"] == ["db2.erp.orders.amount"]

    def test_fabricated_schema_not_tail_folded(self):
        v = self._run("fake.orders")
        assert v["confidence"] == "low"
        assert "db1.erp.orders.amount" in v["s_missing_by_llm"]
        assert "fake.orders.amount" in v["s_extra_by_llm"]

    def test_bare_name_still_resolves_when_unique(self):
        assert self._run("orders")["confidence"] == "high"

    def test_unknown_db_on_chain_side_is_lenient(self):
        from fineprint.synth import cross_validate
        t = {"sources": [{"table": "orders", "schema": "erp", "database": "",
                          "column": "amount"}], "conditions": [], "semantics": []}
        v = cross_validate(t, self._hops("db9.erp.orders"), dict(_CLS), set())
        assert v["confidence"] == "high"   # 链上 db 未知("")时不因 LLM 补了 db 惩罚


class TestFormulaChainVocab:
    """公式必须绑定本指标值链:项目里真实存在但链外的列写进公式 = 错误公式。"""

    _t = {"expr_chain": [{"model": "dm", "column": "gmv", "expr": "sum(order_amt_cny)"}],
          "sources": [{"table": "ods_order", "schema": "ods", "column": "order_amt"}],
          "conditions": [], "semantics": [], "models_visited": ["model.p.dm"]}
    _graph = {"models": {
        "model.p.dm": {"name": "dm", "columns": {"gmv": {}}, "row_set_tables": []},
        "model.p.other": {"name": "other", "columns": {"refund_amt_14d": {}},
                          "row_set_tables": []}},
        "relations": {"sources": {}}}

    def test_out_of_chain_real_column_caught_by_chain_vocab(self):
        from fineprint.synth import build_vocab, verify_freetext
        c_idents, c_nums = build_vocab(self._t, "GMV", None, {}, None)
        assert verify_freetext("sum(refund_amt_14d)", c_idents, c_nums) == ["refund_amt_14d"]

    def test_chain_column_passes_chain_vocab(self):
        from fineprint.synth import build_vocab, verify_freetext
        c_idents, c_nums = build_vocab(self._t, "GMV", None, {}, None)
        assert verify_freetext("sum(order_amt_cny)", c_idents, c_nums) == []

    def test_summary_still_allows_graph_objects(self):
        from fineprint.synth import build_vocab, verify_freetext
        v_idents, v_nums = build_vocab(self._t, "GMV", None, {}, self._graph)
        assert verify_freetext("与 other.refund_amt_14d 口径不同", v_idents, v_nums) == []


class TestRowTopologyGovernance:
    """值来源相同但 join 拓扑不同:不得判确定性重复,立项 row_mismatch。"""

    def _proj(self, tmp_path):
        return make_project(
            tmp_path,
            nodes={"model.p.joined": _node("joined", "compiled/j.sql"),
                   "model.p.plain": _node("plain", "compiled/p.sql")},
            catalog_nodes={"model.p.joined": _cat("main", "joined", {"revenue": "INT"}),
                           "model.p.plain": _cat("main", "plain", {"revenue": "INT"}),
                           "seed.p.a": _cat("main", "a", {"id": "INT", "amount": "INT"}),
                           "seed.p.b": _cat("main", "b", {"id": "INT"})},
            sqls={"compiled/j.sql": ("select sum(a.amount) as revenue from main.a a "
                                     "left join main.b b on a.id = b.id"),
                  "compiled/p.sql": "select sum(a.amount) as revenue from main.a a"})

    def test_sum_over_left_join_not_deterministic_duplicate(self, tmp_path):
        r = scan(build_graph(self._proj(tmp_path)), MLConfig(metrics=[1]))
        assert r["duplicates"] == []
        assert len(r["row_mismatch"]) == 1
        rm = r["row_mismatch"][0]
        assert {rm["a"], rm["b"]} == {"joined.revenue", "plain.revenue"}
        assert rm["rowset_only_a"] == [".main.b"] or rm["rowset_only_b"] == [".main.b"]

    def test_same_topology_still_duplicate(self, tmp_path):
        p = make_project(
            tmp_path,
            nodes={"model.p.m1": _node("m1", "compiled/m1.sql"),
                   "model.p.m2": _node("m2", "compiled/m2.sql")},
            catalog_nodes={"model.p.m1": _cat("main", "m1", {"revenue": "INT"}),
                           "model.p.m2": _cat("main", "m2", {"revenue": "INT"}),
                           "seed.p.a": _cat("main", "a", {"amount": "INT"})},
            sqls={"compiled/m1.sql": "select sum(a.amount) as revenue from main.a a",
                  "compiled/m2.sql": "select sum(a.amount) as revenue from main.a a"})
        r = scan(build_graph(p), MLConfig(metrics=[1]))
        assert len(r["duplicates"]) == 1 and r["row_mismatch"] == []


class TestDriftTargetUid:
    """target 写法归一(短名 ↔ package.model 消歧)不是口径变化。"""

    _base = {"sources": [], "conditions": {}, "semantics": [], "exprs": {}}

    def test_same_uid_different_spelling_no_event(self):
        from fineprint.drift import diff_metric
        old = {**self._base, "target": "m.gmv", "target_uid": "model.p.m.gmv"}
        new = {**self._base, "target": "p.m.gmv", "target_uid": "model.p.m.gmv"}
        assert diff_metric("k", old, new) == []

    def test_uid_change_is_high(self):
        from fineprint.drift import diff_metric
        old = {**self._base, "target": "m.gmv", "target_uid": "model.p.m.gmv"}
        new = {**self._base, "target": "m.gmv", "target_uid": "model.p.m2.gmv"}
        assert [(e["kind"], e["severity"]) for e in diff_metric("k", old, new)] == \
            [("target_changed", "high")]

    def test_legacy_snapshot_falls_back_to_raw_target(self):
        from fineprint.drift import diff_metric
        old = {**self._base, "target": "m.gmv"}
        new = {**self._base, "target": "m2.gmv", "target_uid": "model.p.m2.gmv"}
        assert [e["kind"] for e in diff_metric("k", old, new)] == ["target_changed"]


class TestDocsFullKeys:
    def test_same_name_models_get_package_keys(self, tmp_path):
        n1 = _node("m1", "compiled/a.sql")
        n1["package_name"] = "pkg_a"
        n1["columns"] = {"x": {"name": "x", "description": "A 包的描述"}}
        n2 = _node("m1", "compiled/b.sql", schema="b")
        n2["package_name"] = "pkg_b"
        n2["columns"] = {"x": {"name": "x", "description": "B 包的描述"}}
        p = make_project(tmp_path,
                         nodes={"model.pkg_a.m1": n1, "model.pkg_b.m1": n2},
                         catalog_nodes={"model.pkg_a.m1": _cat("main", "m1", {"x": "INT"}),
                                        "model.pkg_b.m1": _cat("b", "m1", {"x": "INT"})},
                         sqls={"compiled/a.sql": "select 1 as x", "compiled/b.sql": "select 2 as x"})
        docs = p.column_docs
        assert docs["pkg_a:m1"]["x"] == "A 包的描述"
        assert docs["pkg_b:m1"]["x"] == "B 包的描述"

    def test_sources_get_three_part_keys(self, tmp_path):
        srcs = {"source.p.a.orders": {"identifier": "orders", "name": "orders",
                                      "schema": "erp", "database": "db1",
                                      "columns": {"amt": {"name": "amt", "description": "库1"}}},
                "source.p.b.orders": {"identifier": "orders", "name": "orders",
                                      "schema": "erp", "database": "db2",
                                      "columns": {"amt": {"name": "amt", "description": "库2"}}}}
        p = make_project(tmp_path,
                         nodes={"model.p.m1": _node("m1", "compiled/m1.sql")},
                         catalog_nodes={"model.p.m1": _cat("main", "m1", {"x": "INT"})},
                         sqls={"compiled/m1.sql": "select 1 as x"}, sources=srcs)
        docs = p.column_docs
        assert docs["db1.erp.orders"]["amt"] == "库1"
        assert docs["db2.erp.orders"]["amt"] == "库2"


class TestScopeDupAttribution:
    """别名合法复用(跨 scope 同名):非行集条件不得按裸别名归因进值路径。"""

    def _graph(self, tmp_path):
        sql = ("with a as (select amt, id from (select amt, id from main.base) s), "
               "b as (select id from (select id from main.coupons where active = 1) s) "
               "select a.amt as v from a left join b on a.id = b.id")
        p = make_project(
            tmp_path,
            nodes={"model.p.m": _node("m", "compiled/m.sql")},
            catalog_nodes={"model.p.m": _cat("main", "m", {"v": "INT"}),
                           "seed.p.base": _cat("main", "base", {"amt": "INT", "id": "INT"}),
                           "seed.p.coupons": _cat("main", "coupons", {"id": "INT", "active": "INT"})},
            sqls={"compiled/m.sql": sql})
        return build_graph(p)

    def test_left_join_side_condition_not_attributed(self, tmp_path):
        t = trace(self._graph(tmp_path), "m", "v")
        assert not any("active" in c["sql"] for c in t["conditions"])
        assert any("active" in str(c.get("sql")) for c in t["scope_ambiguous"])

    def test_unique_alias_unaffected(self, tmp_path):
        sql = "select s.amt as v from (select amt from main.base where active = 1) s"
        p = make_project(
            tmp_path,
            nodes={"model.p.m": _node("m", "compiled/m.sql")},
            catalog_nodes={"model.p.m": _cat("main", "m", {"v": "INT"}),
                           "seed.p.base": _cat("main", "base", {"amt": "INT", "active": "INT"})},
            sqls={"compiled/m.sql": sql})
        t = trace(build_graph(p), "m", "v")
        assert any("active" in c["sql"] for c in t["conditions"])
        assert t["scope_ambiguous"] == []


class TestAggCaseEquivalence:
    def _one(self, expr):
        node = sqlglot.parse_one(f"select {expr} as n", read="duckdb")
        return agg_one(next(iter(node.find_all(sqlglot.exp.AggFunc))))

    def test_sum_case_01_is_conditional_count(self):
        assert self._one("sum(case when x is not null then 1 else 0 end)") == "count"
        assert self._one("count(x)") == "count"
        assert self._one("sum(case when flag = 1 then 1 end)") == "count"

    def test_sum_of_values_stays_sum(self):
        assert self._one("sum(case when flag = 1 then amt else 0 end)") == "sum"
        assert self._one("sum(amt)") == "sum"


class TestGraphBinding:
    def test_load_graph_stamps_md5_and_snapshot_carries_it(self, tmp_path):
        import json
        from fineprint.tracing import load_graph
        p = make_project(tmp_path,
                         nodes={"model.p.m1": _node("m1", "compiled/m1.sql")},
                         catalog_nodes={"model.p.m1": _cat("main", "m1", {"x": "INT"}),
                                        "seed.p.base": _cat("main", "base", {"amt": "INT"})},
                         sqls={"compiled/m1.sql": "select amt as x from main.base"})
        g = build_graph(p)
        f = tmp_path / "g.json"
        f.write_text(json.dumps(g))
        loaded = load_graph(f)
        assert loaded["meta"]["graph_md5"]
        from fineprint.config import MetricDef
        from fineprint.drift import take_snapshot
        snap = take_snapshot(loaded, MLConfig(metrics=[MetricDef(key="k", title="k", target="m1.x")]))
        assert snap["graph_md5"] == loaded["meta"]["graph_md5"]
        assert snap["metrics"]["k"]["target_uid"] == "model.p.m1.x"
        assert snap["metrics"]["k"]["sources_full3"]
