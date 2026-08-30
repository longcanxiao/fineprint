# -*- coding: utf-8 -*-
"""第四轮外部复审探针固化:内联子查询作用域、跨包/跨 schema 身份、聚合等价类、漂移全名源。"""
import json
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import parse_one

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint.lineage import build_graph, extract_conditions, output_grain, row_scope_closure  # noqa: E402
from fineprint.trace import trace  # noqa: E402

from tests.test_generalize import _cat, _node, make_project  # noqa: E402


class TestSubqueryScope:
    """内联子查询必须有独立 scope:left join 子查询内条件不得进入行集。"""

    def test_left_join_subquery_condition_not_row_level(self):
        sql = ("select count(distinct a.id) as n from orders a "
               "left join (select user_id from coupons where active = 1) c "
               "on a.user_id = c.user_id")
        conds, _ = extract_conditions(parse_one(sql, read="duckdb"), sql, "m")
        active = next(c for c in conds if "active" in c["sql"])
        assert active["scope"] == "c" and active["row_level"] is False

    def test_from_subquery_condition_is_row_level(self):
        sql = "select sum(x) as s from (select x from t where kept = 1) f"
        conds, _ = extract_conditions(parse_one(sql, read="duckdb"), sql, "m")
        kept = next(c for c in conds if "kept" in c["sql"])
        assert kept["scope"] == "f" and kept["row_level"] is True

    def test_inner_join_subquery_in_closure(self):
        sql = ("select a.x from t a join (select id from u where ok = 1) b on a.id = b.id")
        assert "b" in row_scope_closure(parse_one(sql, read="duckdb"))

    def test_grain_descends_into_inline_subquery(self):
        sql = "select dt, total from (select dt, sum(x) as total from t group by dt) s where total > 0"
        assert output_grain(parse_one(sql, read="duckdb")) == ["dt"]


class TestModelIdentity:
    """0.7:主键 = unique_id,跨包同名模型合法共存;短名只是 UI,歧义显式报错。"""

    def _pkg_node(self, name, sql_rel, pkg, schema="main"):
        n = _node(name, sql_rel, schema=schema)
        n["package_name"] = pkg
        return n

    def _two_pkg_project(self, tmp_path, **kw):
        return make_project(
            tmp_path,
            nodes={"model.pkg_a.orders": self._pkg_node("orders", "compiled/a.sql", "pkg_a"),
                   "model.pkg_b.orders": self._pkg_node("orders", "compiled/b.sql", "pkg_b",
                                                        schema="b")},
            catalog_nodes={"model.pkg_a.orders": _cat("main", "orders", {"x": "INT"}),
                           "model.pkg_b.orders": _cat("b", "orders", {"x": "INT"})},
            sqls={"compiled/a.sql": "select 1 as x", "compiled/b.sql": "select 2 as x"}, **kw)

    def test_same_name_coexists_and_bare_ref_is_ambiguous(self, tmp_path):
        from fineprint.trace import resolve_model
        p = self._two_pkg_project(tmp_path)   # root 未知 → 全按一方:两模型以 uid 共存
        assert set(p.models) == {"model.pkg_a.orders", "model.pkg_b.orders"}
        g = build_graph(p)
        with pytest.raises(KeyError, match="歧义"):
            resolve_model(g, "orders")

    def test_qualified_forms_resolve(self, tmp_path):
        from fineprint.trace import display_name, resolve_model
        g = build_graph(self._two_pkg_project(tmp_path))
        assert resolve_model(g, "pkg_a.orders") == "model.pkg_a.orders"
        assert resolve_model(g, "pkg_b:orders") == "model.pkg_b.orders"
        assert resolve_model(g, "model.pkg_a.orders") == "model.pkg_a.orders"
        assert display_name(g, "model.pkg_a.orders") == "pkg_a:orders"   # 重名 → 带包展示
        t = trace(g, "pkg_a.orders", "x")
        assert t["expr_chain"][0]["model"] == "pkg_a:orders"
        assert t["expr_chain"][0]["model_uid"] == "model.pkg_a.orders"

    def test_unique_short_name_still_plain(self, tmp_path):
        from fineprint.trace import display_name
        n = _node("m1", "compiled/m1.sql")
        p = make_project(tmp_path, nodes={"model.p.m1": n},
                         catalog_nodes={"model.p.m1": _cat("main", "m1", {"x": "INT"})},
                         sqls={"compiled/m1.sql": "select 1 as x"})
        g = build_graph(p)
        assert display_name(g, "model.p.m1") == "m1"
        assert trace(g, "m1", "x")["target"].endswith(".m1.x")


class TestExternalPackages:
    """第三方包模型 = 数据源边界(与 ODS 同约定):不解析其 SQL/注释,血缘在物化表截止。"""

    def _proj(self, tmp_path, **kw):
        own = _node("m1", "compiled/m1.sql")
        own["package_name"] = "p"
        ext = _node("ext_orders", "compiled/absent.sql", schema="vendor")
        ext["package_name"] = "pkg_x"
        ext["columns"] = {"amt": {"name": "amt", "description": "包作者写的注释"}}
        return make_project(tmp_path,
                            nodes={"model.p.m1": own, "model.pkg_x.ext_orders": ext},
                            catalog_nodes={
                                "model.p.m1": _cat("main", "m1", {"x": "INT"}),
                                "model.pkg_x.ext_orders": _cat("vendor", "ext_orders", {"amt": "INT"})},
                            sqls={"compiled/m1.sql": "select amt as x from vendor.ext_orders"},
                            project_name="p", **kw)

    def test_external_model_folds_to_source(self, tmp_path):
        p = self._proj(tmp_path)
        assert "model.pkg_x.ext_orders" not in p.models             # SQL 缺失也不报错:根本不读
        assert "model.p.m1" in p.models
        assert p.external_models[".vendor.ext_orders"]["package"] == "pkg_x"
        assert p.source_by_relation[".vendor.ext_orders"] == "ext_orders"
        g = build_graph(p)
        assert "model.pkg_x.ext_orders" not in g["models"]
        assert g["relations"]["external"][".vendor.ext_orders"] == {"name": "ext_orders",
                                                                    "package": "pkg_x"}
        t = trace(g, "m1", "x")
        assert t["sources"] == [{"table": "ext_orders", "schema": "vendor", "database": "",
                                 "column": "amt", "package": "pkg_x"}]

    def test_external_docs_excluded_from_llm_context(self, tmp_path):
        assert "ext_orders" not in self._proj(tmp_path).column_docs   # 包作者文本不进 docs_ctx

    def test_target_on_external_model_gets_boundary_hint(self, tmp_path):
        g = build_graph(self._proj(tmp_path))
        with pytest.raises(KeyError, match="internal_packages"):
            trace(g, "ext_orders", "amt")

    def test_internal_packages_via_yml_sees_through(self, tmp_path):
        (tmp_path / "proj").mkdir()
        (tmp_path / "proj" / "fineprint.yml").write_text("internal_packages: [pkg_x]\n")
        own = _node("m1", "compiled/m1.sql")
        own["package_name"] = "p"
        ext = _node("ext_orders", "compiled/ext.sql", schema="vendor")
        ext["package_name"] = "pkg_x"
        p = make_project(tmp_path,
                         nodes={"model.p.m1": own, "model.pkg_x.ext_orders": ext},
                         catalog_nodes={
                             "model.p.m1": _cat("main", "m1", {"x": "INT"}),
                             "model.pkg_x.ext_orders": _cat("vendor", "ext_orders", {"amt": "INT"})},
                         sqls={"compiled/m1.sql": "select amt as x from vendor.ext_orders",
                               "compiled/ext.sql": "select 1 as amt"},
                         project_name="p")
        assert "model.pkg_x.ext_orders" in p.models and p.external_models == {}


class TestSourceIdentity:
    def _two_schema_sources(self):
        return {"source.p.erp.orders": {"identifier": "orders", "name": "orders",
                                        "schema": "erp", "database": "db"},
                "source.p.crm.orders": {"identifier": "orders", "name": "orders",
                                        "schema": "crm", "database": "db"}}

    def test_same_identifier_different_schema_is_legal(self, tmp_path):
        p = make_project(tmp_path,
                         nodes={"model.p.m1": _node("m1", "compiled/m1.sql")},
                         catalog_nodes={"model.p.m1": _cat("main", "m1", {"x": "INT"})},
                         sqls={"compiled/m1.sql": "select 1 as x"},
                         sources=self._two_schema_sources())
        assert set(p.sources) == {"db.erp.orders", "db.crm.orders"}     # 物理三段键
        assert p.source_by_relation == {"db.erp.orders": "orders", "db.crm.orders": "orders"}

    def test_fingerprint_distinguishes_schema(self):
        from fineprint.governance import fingerprint_of
        t1 = {"sources": [{"table": "orders", "schema": "erp", "column": "amount"}], "conditions": []}
        t2 = {"sources": [{"table": "orders", "schema": "crm", "column": "amount"}], "conditions": []}
        assert fingerprint_of(t1) != fingerprint_of(t2)


class TestAggEquivalence:
    def _sig(self, expr):
        from fineprint.lineage import agg_one as _agg_one
        node = sqlglot.parse_one(expr, read="duckdb")
        return {_agg_one(f) for f in node.find_all(sqlglot.exp.AggFunc)}

    def test_rowcount_class_unified(self):
        assert self._sig("count(*)") == self._sig("count(1)") == self._sig("sum(1)") == {"rowcount"}

    def test_count_column_stays_distinct_from_rowcount(self):
        assert self._sig("count(x)") == {"count"}
        assert self._sig("count(distinct x)") == {"count:distinct"}
        assert self._sig("sum(x)") == {"sum"}


class TestFreetextLexicon:
    """展示层自由文本(公式/定义/告诫)的字段引用与口径数字必须可溯源。"""

    def _vocab(self):
        from fineprint.synth import build_vocab
        t = {"expr_chain": [{"model": "dm_stats", "column": "refund_amt_14d",
                             "expr": "sum(refund_amt)"}],
             "sources": [{"table": "ods_refund", "schema": "ods", "column": "refund_amt"}],
             "conditions": [{"sql": "date_diff <= 14"}], "semantics": [],
             "models_visited": ["dm_stats"]}
        return build_vocab(t, "退款率", None, {})

    def test_real_references_pass(self):
        from fineprint.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("sum(dm_stats.refund_amt_14d) 窗口 14 天", idents, nums) == []

    def test_fabricated_column_caught(self):
        from fineprint.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("sum(fake_table.fake_col)", idents, nums)

    def test_fabricated_number_caught(self):
        from fineprint.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("限 15 天内的退款", idents, nums) == ["15"]

    def test_plain_prose_not_flagged(self):
        from fineprint.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("Total payment amount attributed to each customer.",
                               idents, nums) == []

    def test_single_digit_window_caught(self):
        from fineprint.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("限 7 天内退款", idents, nums) == ["7"]      # 篡改窗口
        assert verify_freetext("限 14 天内退款", idents, nums) == []        # 真实窗口

    def test_prose_formula_without_aggregation_caught(self):
        from fineprint.synth import formula_agg_check
        assert formula_agg_check("退款金额除以支付人数", {"sum", "count:distinct"})

    def test_fabricated_aggregation_caught(self):
        from fineprint.synth import formula_agg_check
        assert formula_agg_check("count(distinct order_id)", {"sum"})

    def test_matching_aggregation_passes(self):
        from fineprint.synth import formula_agg_check
        assert formula_agg_check("sum(refund_amt) / sum(pay_amt)", {"sum"}) == []
        assert formula_agg_check("count(*)", {"rowcount"}) == []
        assert formula_agg_check("avg(x)", {"sum", "count"}) == []          # 展开形豁免
        assert formula_agg_check("任意散文", set()) == []                    # 缺证不下结论


class TestJoinCountQuality:
    """join 上的行数型聚合 = 口径含义未自证的 SQL 质量问题,须确定性立项。"""

    def _sems(self, sql):
        _, sems = extract_conditions(parse_one(sql, read="duckdb"), sql, "m")
        return [s for s in sems if s["type"] == "join_count"]

    def test_left_join_rowcount_flagged_with_full_info(self):
        sql = ("select count(*) as n from erp.orders o "
               "left join erp.order_items i on o.order_id = i.order_id")
        got = self._sems(sql)
        assert len(got) == 1
        assert got[0]["column"] == "n"
        assert set(got[0]["tables"]) == {"erp.orders", "erp.order_items"}
        assert any("order_id" in k for k in got[0]["join_keys"])

    def test_join_inside_cte_propagates(self):
        sql = ("with base as (select o.id from orders o join items i on o.id = i.order_id) "
               "select count(*) as n from base")
        assert len(self._sems(sql)) == 1

    def test_no_flag_cases(self):
        assert self._sems("select count(*) as n from orders") == []                # 单表无歧义
        assert self._sems("select count(distinct o.id) as n from orders o "
                          "join items i on o.id = i.order_id") == []               # 计数对象自证
        assert self._sems("select count(*) over () as n, x from orders o "
                          "join items i on o.id = i.order_id") == []               # 窗口计数非行数聚合
        assert self._sems("select sum(o.amt) as s from orders o "
                          "join items i on o.id = i.order_id") == []               # 非行数型

    def test_scan_reports_sql_quality(self):
        from fineprint.config import MLConfig
        from fineprint.governance import scan
        graph = {"models": {"m": {"layer": "dm", "columns": {}, "conditions": [],
                                  "semantics": [{"type": "join_count", "column": "n", "line": 3,
                                                 "tables": ["a", "b"], "join_keys": ["a.k = b.k"]}]}},
                 "relations": {"models": {}, "sources": {}}}
        cfg = MLConfig(metrics=[1])
        r = scan(graph, cfg)
        assert len(r["sql_quality"]) == 1 and r["sql_quality"][0]["model"] == "m"
        assert scan(graph, MLConfig(metrics=[1], scan_layers=["app"]))["sql_quality"] == []


class TestConfigContract:
    def _load(self, tmp_path, body):
        from fineprint.config import MLConfig
        (tmp_path / "fineprint.yml").write_text(body)
        return MLConfig.load(tmp_path)

    def test_negative_max_llm_pairs_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="max_llm_pairs"):
            self._load(tmp_path, "language: zh\nmetrics:\n  - key: a\n    target: m.c\n"
                                 "governance:\n  max_llm_pairs: -1\n")

    def test_string_extra_targets_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="extra_targets"):
            self._load(tmp_path, "language: zh\nmetrics:\n  - key: a\n    target: m.c\n"
                                 "    extra_targets: m2.d\n")

    def test_bad_scan_layers_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="scan_layers"):
            self._load(tmp_path, "language: zh\nmetrics:\n  - key: a\n    target: m.c\n"
                                 "governance:\n  scan_layers: dm\n")

    def test_case_insensitive_key_collision_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="重复"):
            self._load(tmp_path, "language: zh\nmetrics:\n  - key: GMV\n    target: m.c\n"
                                 "  - key: gmv\n    target: m.d\n")

    def test_toplevel_list_clear_error(self, tmp_path):
        with pytest.raises(ValueError, match="顶层须为映射"):
            self._load(tmp_path, "- a\n- b\n")

    def test_malformed_target_rejected(self, tmp_path):
        # a.b.c 是合法三段(package.model.column);四段起才是格式错误
        for bad in ("a.b.c.d", "m.", ".", "m c.x"):
            with pytest.raises(ValueError, match="model.column"):
                self._load(tmp_path, f"language: zh\nmetrics:\n  - key: a\n    target: {json.dumps(bad)}\n")

    def test_internal_packages_must_be_str_list(self, tmp_path):
        for bad in ("internal_packages: shared\n", "internal_packages: [1, 2]\n"):
            with pytest.raises(ValueError, match="internal_packages"):
                self._load(tmp_path, "language: zh\nmetrics:\n  - key: a\n    target: m.c\n" + bad)


class TestDriftSourcesFull:
    _old = {"target": "m.c", "sources": ["orders.amount"],
            "conditions": {}, "semantics": [], "exprs": {}}

    def test_legacy_snapshot_no_false_positive(self):
        from fineprint.drift import diff_metric
        new = {**self._old, "sources_full": ["erp.orders.amount"]}
        assert diff_metric("k", dict(self._old), new) == []   # 老快照缺 full → 回退裸名

    def test_cross_schema_repoint_detected(self):
        from fineprint.drift import diff_metric
        old = {**self._old, "sources_full": ["erp.orders.amount"]}
        new = {**self._old, "sources_full": ["crm.orders.amount"]}
        kinds = {(e["kind"], e["severity"]) for e in diff_metric("k", old, new)}
        assert ("source_removed", "high") in kinds and ("source_added", "high") in kinds

    def test_cross_database_repoint_detected_with_full3(self):
        """0.7 物理三段版:跨 database 改指向(schema.table 不变)也可检出。"""
        from fineprint.drift import diff_metric
        base = {**self._old, "sources_full": ["erp.orders.amount"]}
        old = {**base, "sources_full3": ["db1.erp.orders.amount"]}
        new = {**base, "sources_full3": ["db2.erp.orders.amount"]}
        kinds = {(e["kind"], e["severity"]) for e in diff_metric("k", old, new)}
        assert ("source_removed", "high") in kinds and ("source_added", "high") in kinds
        # 老基线缺 full3 → 回退 sources_full,不误报
        assert diff_metric("k", dict(base), dict(new)) == []
