# -*- coding: utf-8 -*-
"""join/分组上下文进通道一视野(第二类血缘的可见化)。

值链之外的 join 伙伴/分组维表塑造行集与粒度但不供值——trace 输出
context_tables(模型上下文带列清单);互验把落在上下文里的 LLM 引用
从 s_extra(幻觉惩罚)改记 s_context_by_llm(合法上下文,不惩罚),
模型上下文做列存在性校验,列不存在仍是幻觉。指纹/漂移快照不受影响。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint.governance import fingerprint_of  # noqa: E402
from fineprint.lineage import build_graph  # noqa: E402
from fineprint.synth import cross_validate  # noqa: E402
from fineprint.trace import trace  # noqa: E402
from tests.test_generalize import _cat, _node, make_project  # noqa: E402

CLS = {"f1_fps": {}, "f2_fps": set(), "quote_fail": 0,
       "suspect": [], "out_of_scope": [], "unparsed": []}


def _graph(tmp_path):
    """fct 按链外维表分组聚合:dim_channel(真实表)与 geo_dim(模型)都只塑造
    行集/分组,不供 gmv 的值。"""
    p = make_project(
        tmp_path,
        nodes={"model.p.fct": _node("fct", "compiled/fct.sql"),
               "model.p.geo_dim": _node("geo_dim", "compiled/geo.sql")},
        catalog_nodes={
            "model.p.fct": _cat("main", "fct", {"gmv": "HUGEINT"}),
            "model.p.geo_dim": _cat("main", "geo_dim",
                                    {"gid": "INT", "province": "TEXT"}),
            "seed.p.orders": _cat("main", "orders",
                                  {"amt": "INT", "ch_id": "INT", "gid": "INT"}),
            "seed.p.dim_channel": _cat("main", "dim_channel",
                                       {"id": "INT", "channel_name": "TEXT"}),
            "seed.p.raw_geo": _cat("main", "raw_geo",
                                   {"gid": "INT", "province": "TEXT"})},
        sqls={"compiled/fct.sql": (
                  "select sum(o.amt) as gmv from main.orders o "
                  "join main.dim_channel d on o.ch_id = d.id "
                  "join main.geo_dim g on o.gid = g.gid "
                  "group by d.channel_name, g.province"),
              "compiled/geo.sql": "select gid, province from main.raw_geo"})
    return build_graph(p)


class TestContextTables:
    def test_offchain_partners_visible(self, tmp_path):
        t = trace(_graph(tmp_path), "fct", "gmv")
        ctx = {c["table"]: c for c in t["context_tables"]}
        assert ".main.dim_channel" in ctx          # 真实表伙伴:表身份
        assert ctx[".main.dim_channel"].get("columns") is None
        assert ".main.geo_dim" in ctx              # 模型伙伴:带展示名与列清单
        assert ctx[".main.geo_dim"]["model"] == "geo_dim"
        assert ctx[".main.geo_dim"]["columns"] == ["gid", "province"]
        # 值链源表不入上下文
        assert ".main.orders" not in ctx

    def test_chain_model_not_context(self, tmp_path):
        """值链上游模型已在值链视野,不重复入上下文。"""
        g = _graph(tmp_path)
        t = trace(g, "geo_dim", "province")
        assert t["context_tables"] == []

    def test_fingerprint_unaffected(self, tmp_path):
        g = _graph(tmp_path)
        t = trace(g, "fct", "gmv")
        fp1 = fingerprint_of(t)
        t2 = dict(t, context_tables=[])
        assert fingerprint_of(t2) == fp1


class TestContextCrossValidate:
    def _val(self, tmp_path, claimed_table, claimed_col):
        t = trace(_graph(tmp_path), "fct", "gmv")
        hops = {"model.p.fct": {"columns": {"gmv": {"source_columns": [
            {"table": "main.orders", "column": "amt"},
            {"table": claimed_table, "column": claimed_col}]}}}}
        return cross_validate(t, hops, dict(CLS), set(), set())

    def test_context_reference_not_penalized(self, tmp_path):
        v = self._val(tmp_path, "main.dim_channel", "channel_name")
        assert v["s_context_by_llm"] == ["main.dim_channel.channel_name"]
        assert v["s_extra_by_llm"] == []
        assert v["confidence"] == "high"

    def test_model_context_checks_column_exists(self, tmp_path):
        """模型上下文带列清单:真实列豁免,编造列仍是幻觉。"""
        ok = self._val(tmp_path / "a", "main.geo_dim", "province")
        assert ok["s_context_by_llm"] == ["main.geo_dim.province"]
        bad = self._val(tmp_path / "b", "main.geo_dim", "made_up_col")
        assert bad["s_context_by_llm"] == []
        assert bad["s_extra_by_llm"] == ["main.geo_dim.made_up_col"]

    def test_unrelated_table_still_extra(self, tmp_path):
        v = self._val(tmp_path, "main.nowhere", "x")
        assert v["s_context_by_llm"] == []
        assert v["s_extra_by_llm"] == ["main.nowhere.x"]
