# -*- coding: utf-8 -*-
"""0.8 双写赛马:确定性公式组合器(render)回归。

组合规则:作用域展开 / 聚合边界(def + grain)/ union 一致性 / fail-closed;
round-trip:链内词表 + 聚合锚点 + 叶子源集与通道一互证;
race:agree / consistent / prose / disagree / renderer_unsupported;
发布状态机:VERIFIED / TECHNICAL_ONLY / REVIEW_REQUIRED 与 rt_failed 阻断。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from metriclens.governance import agg_signature  # noqa: E402
from metriclens.lineage import build_graph  # noqa: E402
from metriclens.render import (  # noqa: E402
    _Composer,
    build_facts,
    publication_status,
    race_formula,
)
from metriclens.synth import build_vocab, merged_trace  # noqa: E402
from metriclens.trace import trace  # noqa: E402
from tests.test_generalize import _cat, _node, make_project  # noqa: E402


def mk(tmp_path, sqls_by_model, cats, seed_cols):
    nodes = {f"model.p.{name}": _node(name, f"compiled/{name}.sql")
             for name in sqls_by_model}
    catalog = {"seed.p.raw_orders": _cat("main", "raw_orders", seed_cols)}
    for name, cols in cats.items():
        catalog[f"model.p.{name}"] = _cat("main", name, cols)
    proj = make_project(
        tmp_path, nodes=nodes, catalog_nodes=catalog,
        sqls={f"compiled/{name}.sql": sql for name, sql in sqls_by_model.items()},
        project_name="p")
    return proj, build_graph(proj)


SEED = {"id": "INTEGER", "user_id": "INTEGER", "amount": "INTEGER",
        "status": "VARCHAR", "dt": "DATE"}


def facts_of(proj, graph, targets, t, link_aggs=None):
    cv = build_vocab(t, "t", None, {}, None)
    if link_aggs is None:
        link_aggs = set(agg_signature(t))
        for mo in t["models_visited"]:
            link_aggs |= set(graph["models"].get(mo, {}).get("agg_fns") or [])
    return build_facts(proj, graph, t, targets, cv, link_aggs)


class TestComposeBasics:
    def test_cross_model_inline_and_leaves(self, tmp_path):
        """跨模型展开:sum(pay_amt) 内联上游 case-when,叶子=源表列。"""
        proj, graph = mk(
            tmp_path,
            {"stg": ("select id, case when status = 'paid' then amount else 0 end as pay_amt "
                     "from main.raw_orders where status <> 'test'"),
             "fct": "select sum(pay_amt) as gmv from main.stg"},
            {"stg": {"id": "INTEGER", "pay_amt": "INTEGER"},
             "fct": {"gmv": "HUGEINT"}}, SEED)
        c = _Composer(proj, graph).compose_target("model.p.fct", "gmv")
        assert c["status"] == "proven"
        assert c["top"].upper().startswith("SUM(CASE")
        assert "raw_orders.amount" in c["top"] and "raw_orders.status" in c["top"]
        assert not c["defs"]
        assert (".main.raw_orders", "amount") in set(map(tuple, c["leaf_pairs"]))

    def test_passthrough_collapses(self, tmp_path):
        proj, graph = mk(
            tmp_path,
            {"stg": "select id, amount from main.raw_orders",
             "mid": "select id, amount from main.stg",
             "fct": "select sum(amount) as total from main.mid"},
            {"stg": {"id": "INTEGER", "amount": "INTEGER"},
             "mid": {"id": "INTEGER", "amount": "INTEGER"},
             "fct": {"total": "HUGEINT"}}, SEED)
        c = _Composer(proj, graph).compose_target("model.p.fct", "total")
        assert c["status"] == "proven"
        assert c["top"].upper() == "SUM(RAW_ORDERS.AMOUNT)".upper()


class TestAggBoundary:
    def test_join_context_makes_def_with_grain(self, tmp_path):
        """join 改粒度的消费位:聚合定义不内联,落 def 并带定义处 grain。"""
        proj, graph = mk(
            tmp_path,
            {"fct": ("with usr as (select user_id, sum(amount) as total "
                     "from main.raw_orders group by 1) "
                     "select o.id, u.total as user_total from main.raw_orders o "
                     "join usr u on o.user_id = u.user_id")},
            {"fct": {"id": "INTEGER", "user_total": "HUGEINT"}}, SEED)
        c = _Composer(proj, graph).compose_target("model.p.fct", "user_total")
        assert c["status"] == "proven"
        assert len(c["defs"]) == 1
        d = c["defs"][0]
        assert d["kind"] == "agg" and d["grain"] == ["user_id"] and d["join_context"]
        assert c["top"] == d["name"]

    def test_upstream_agg_inside_agg_is_def_and_inline_invalid(self, tmp_path):
        """上游聚合列再进聚合:必须 def(SUM(SUM) 非法),inline 置 None。"""
        proj, graph = mk(
            tmp_path,
            {"day": ("select dt, sum(amount) as day_amt from main.raw_orders group by 1"),
             "fct": "select max(day_amt) as peak from main.day"},
            {"day": {"dt": "DATE", "day_amt": "HUGEINT"},
             "fct": {"peak": "HUGEINT"}}, SEED)
        c = _Composer(proj, graph).compose_target("model.p.fct", "peak")
        assert c["status"] == "proven"
        assert [d["kind"] for d in c["defs"]] == ["agg"]
        assert c["defs"][0]["grain"] == ["dt"]
        assert c["inline"] is None          # 回代形含嵌套聚合,不可作展示公式
        assert c["top"].upper() == "MAX(DAY_AMT)"


class TestUnion:
    def test_union_consistent_composes(self, tmp_path):
        proj, graph = mk(
            tmp_path,
            {"fct": ("select amount as v from main.raw_orders "
                     "union all select amount as v from main.raw_orders")},
            {"fct": {"v": "INTEGER"}}, SEED)
        c = _Composer(proj, graph).compose_target("model.p.fct", "v")
        assert c["status"] == "proven"

    def test_union_divergent_becomes_union_def(self, tmp_path):
        """UNION 分支定义不一致(跨源 rollup 常态):逐分支保留为 union def,
        不放弃也绝不挑一支;分支带标签与各自表达式。"""
        proj, graph = mk(
            tmp_path,
            {"fct": ("select amount as v from main.raw_orders "
                     "union all select id as v from main.raw_orders")},
            {"fct": {"v": "INTEGER"}}, SEED)
        c = _Composer(proj, graph).compose_target("model.p.fct", "v")
        assert c["status"] == "proven"
        assert c["top"] == "v"
        d = c["defs"][0]
        assert d["kind"] == "union" and len(d["branches"]) == 2
        assert {b["expr"] for b in d["branches"]} == {"raw_orders.amount", "raw_orders.id"}
        assert all(b["label"] == "raw_orders" for b in d["branches"])
        assert (".main.raw_orders", "id") in set(map(tuple, c["leaf_pairs"]))


class TestRoundTrip:
    def test_green_chain_proven(self, tmp_path):
        proj, graph = mk(
            tmp_path,
            {"stg": "select id, amount from main.raw_orders where status = 'paid'",
             "fct": "select sum(amount) as gmv from main.stg"},
            {"stg": {"id": "INTEGER", "amount": "INTEGER"},
             "fct": {"gmv": "HUGEINT"}}, SEED)
        t = trace(graph, "fct", "gmv")
        facts = facts_of(proj, graph, [("model.p.fct", "gmv")], t)
        f = facts["formula"]
        assert f["status"] == "proven" and not f["rt_failed"]

    def test_agg_anchor_mismatch_fails_roundtrip(self, tmp_path):
        """错误的链路聚合签名 → 组合公式过不了自己的锚点校验 → ambiguous+rt_failed。"""
        proj, graph = mk(
            tmp_path,
            {"fct": "select sum(amount) as gmv from main.raw_orders"},
            {"fct": {"gmv": "HUGEINT"}}, SEED)
        t = trace(graph, "fct", "gmv")
        facts = facts_of(proj, graph, [("model.p.fct", "gmv")], t,
                         link_aggs={"count:distinct"})
        f = facts["formula"]
        assert f["status"] == "ambiguous" and f["rt_failed"]
        assert publication_status("high", facts, {"verdict": "agree"}) == "REVIEW_REQUIRED"


class TestMultiTarget:
    def test_multi_target_ambiguous_with_per_target(self, tmp_path):
        proj, graph = mk(
            tmp_path,
            {"fct": ("select sum(amount) as num, count(distinct user_id) as den "
                     "from main.raw_orders")},
            {"fct": {"num": "HUGEINT", "den": "BIGINT"}}, SEED)
        m = SimpleNamespace(target="fct.num", extra_targets=["fct.den"], query_filter=None)
        t = merged_trace(graph, m)
        facts = facts_of(proj, graph, [("model.p.fct", "num"), ("model.p.fct", "den")], t)
        f = facts["formula"]
        assert f["status"] == "ambiguous" and f["top"] is None
        assert len(f["per_target"]) == 2
        assert any("多目标" in r for r in f["reasons"])


class TestRace:
    def _facts(self, tmp_path):
        proj, graph = mk(
            tmp_path,
            {"stg": ("select id, user_id, case when status = 'paid' then amount else 0 end "
                     "as pay_amt from main.raw_orders"),
             "fct": "select sum(pay_amt) as gmv from main.stg"},
            {"stg": {"id": "INTEGER", "user_id": "INTEGER", "pay_amt": "INTEGER"},
             "fct": {"gmv": "HUGEINT"}}, SEED)
        t = trace(graph, "fct", "gmv")
        return facts_of(proj, graph, [("model.p.fct", "gmv")], t)

    def test_agree_modulo_qualifiers_and_case(self, tmp_path):
        facts = self._facts(tmp_path)
        llm = "SUM(case when o.status = 'paid' then o.amount else 0 end)"
        assert race_formula(facts, llm, {})["verdict"] == "agree"

    def test_consistent_when_different_but_no_contradiction(self, tmp_path):
        facts = self._facts(tmp_path)
        llm = "sum(case when status in ('paid') then amount else 0 end)"
        assert race_formula(facts, llm, {})["verdict"] == "consistent"

    def test_prose_unparseable(self, tmp_path):
        facts = self._facts(tmp_path)
        assert race_formula(facts, "支付成功订单的金额合计(sum)", {})["verdict"] == "prose"

    def test_disagree_on_machine_contradiction(self, tmp_path):
        facts = self._facts(tmp_path)
        r = race_formula(facts, "max(binlog_ts)", {"formula_aggs": ["公式聚合 ['max'] 不在链路聚合中"]})
        assert r["verdict"] == "disagree"
        assert "formula_aggs" in r["detail"]

    def test_renderer_unsupported_verdict(self, tmp_path):
        facts = self._facts(tmp_path)
        facts["formula"]["status"] = "unsupported"
        assert race_formula(facts, "sum(x)", {})["verdict"] == "renderer_unsupported"

    def test_def_body_candidate_for_passthrough(self, tmp_path):
        """top 是单一 def 名时,def 体是合法比对候选(LLM 在该粒度写公式)。"""
        proj, graph = mk(
            tmp_path,
            {"fct": ("with usr as (select user_id, sum(amount) as total "
                     "from main.raw_orders group by 1) "
                     "select o.id, u.total as user_total from main.raw_orders o "
                     "join usr u on o.user_id = u.user_id")},
            {"fct": {"id": "INTEGER", "user_total": "HUGEINT"}}, SEED)
        t = trace(graph, "fct", "user_total")
        facts = facts_of(proj, graph, [("model.p.fct", "user_total")], t)
        assert race_formula(facts, "SUM(o.amount)", {})["verdict"] == "agree"


class TestPublicationStatus:
    def _facts(self, formula_status="proven", kf_status="proven", rt=False):
        return {"formula": {"status": formula_status, "rt_failed": rt},
                "key_filters": {"status": kf_status}}

    def test_matrix(self):
        ok = {"verdict": "agree"}
        assert publication_status("high", self._facts(), ok) == "VERIFIED"
        assert publication_status("medium", self._facts(), ok) == "TECHNICAL_ONLY"
        assert publication_status("medium", self._facts("unsupported"), ok) == "REVIEW_REQUIRED"
        assert publication_status("high", self._facts(), {"verdict": "disagree"}) == "REVIEW_REQUIRED"
        assert publication_status("high", self._facts(kf_status="ambiguous"), ok) == "REVIEW_REQUIRED"
        assert publication_status("high", self._facts(rt=True), ok) == "REVIEW_REQUIRED"
        assert publication_status("high", self._facts(), {"verdict": "renderer_unsupported"}) == "VERIFIED"
