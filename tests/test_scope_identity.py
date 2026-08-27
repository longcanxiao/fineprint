# -*- coding: utf-8 -*-
"""第四轮外部复审探针固化:内联子查询作用域、跨包/跨 schema 身份、聚合等价类、漂移全名源。"""
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import parse_one

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from metriclens.lineage import extract_conditions, output_grain, row_scope_closure  # noqa: E402

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
    def test_cross_package_same_name_raises(self, tmp_path):
        n1 = _node("orders", "compiled/a.sql")
        n1["package_name"] = "pkg_a"
        n2 = _node("orders", "compiled/b.sql")
        n2["package_name"] = "pkg_b"
        p = make_project(tmp_path,
                         nodes={"model.pkg_a.orders": n1, "model.pkg_b.orders": n2},
                         catalog_nodes={"model.pkg_a.orders": _cat("main", "orders", {"x": "INT"})},
                         sqls={"compiled/a.sql": "select 1 as x", "compiled/b.sql": "select 2 as x"})
        with pytest.raises(ValueError, match="折叠冲突"):
            _ = p.models


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
        assert set(p.sources) == {"erp.orders", "crm.orders"}
        assert p.source_by_relation == {"erp.orders": "orders", "crm.orders": "orders"}

    def test_fingerprint_distinguishes_schema(self):
        from metriclens.governance import fingerprint_of
        t1 = {"sources": [{"table": "orders", "schema": "erp", "column": "amount"}], "conditions": []}
        t2 = {"sources": [{"table": "orders", "schema": "crm", "column": "amount"}], "conditions": []}
        assert fingerprint_of(t1) != fingerprint_of(t2)


class TestAggEquivalence:
    def _sig(self, expr):
        from metriclens.governance import _agg_one
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
        from metriclens.synth import build_vocab
        t = {"expr_chain": [{"model": "dm_stats", "column": "refund_amt_14d",
                             "expr": "sum(refund_amt)"}],
             "sources": [{"table": "ods_refund", "schema": "ods", "column": "refund_amt"}],
             "conditions": [{"sql": "date_diff <= 14"}], "semantics": [],
             "models_visited": ["dm_stats"]}
        return build_vocab(t, "退款率", None, {}, {})

    def test_real_references_pass(self):
        from metriclens.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("sum(dm_stats.refund_amt_14d) 窗口 14 天", idents, nums) == []

    def test_fabricated_column_caught(self):
        from metriclens.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("sum(fake_table.fake_col)", idents, nums)

    def test_fabricated_number_caught(self):
        from metriclens.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("限 15 天内的退款", idents, nums) == ["15"]

    def test_plain_prose_not_flagged(self):
        from metriclens.synth import verify_freetext
        idents, nums = self._vocab()
        assert verify_freetext("Total payment amount attributed to each customer.",
                               idents, nums) == []


class TestConfigContract:
    def _load(self, tmp_path, body):
        from metriclens.config import MLConfig
        (tmp_path / "metriclens.yml").write_text(body)
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


class TestDriftSourcesFull:
    _old = {"target": "m.c", "sources": ["orders.amount"],
            "conditions": {}, "semantics": [], "exprs": {}}

    def test_legacy_snapshot_no_false_positive(self):
        from metriclens.drift import diff_metric
        new = {**self._old, "sources_full": ["erp.orders.amount"]}
        assert diff_metric("k", dict(self._old), new) == []   # 老快照缺 full → 回退裸名

    def test_cross_schema_repoint_detected(self):
        from metriclens.drift import diff_metric
        old = {**self._old, "sources_full": ["erp.orders.amount"]}
        new = {**self._old, "sources_full": ["crm.orders.amount"]}
        kinds = {(e["kind"], e["severity"]) for e in diff_metric("k", old, new)}
        assert ("source_removed", "high") in kinds and ("source_added", "high") in kinds
