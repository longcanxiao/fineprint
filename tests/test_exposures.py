# -*- coding: utf-8 -*-
"""dbt exposures 自动发现:消费方声明进入卡片/漂移/治理/init 预填。

exposure 依赖是模型级——消费方标注挂在指标出口模型上;source/第三方依赖
不入(数据源边界)。漂移事件带受影响看板名单;治理结对带两侧消费方加权;
init 从 exposures 圈出口模型的数值度量列为注释候选(最后圈列仍须人工)。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint.cli import _exposure_candidates  # noqa: E402
from fineprint.config import MLConfig  # noqa: E402
from fineprint.drift import annotate_exposures  # noqa: E402
from fineprint.governance import scan  # noqa: E402
from fineprint.lineage import build_graph  # noqa: E402
from fineprint.synth import target_exposures  # noqa: E402
from tests.test_generalize import _cat, _node, make_project  # noqa: E402

EXPO = {
    "exposure.p.weekly_board": {
        "name": "weekly_board", "label": "大盘周报", "type": "dashboard",
        "url": "https://bi.example.com/42", "maturity": "high",
        "owner": {"name": "数据团队", "email": None},
        "depends_on": {"nodes": ["model.p.m1", "source.p.raw.orders"]}},
    "exposure.p.ml_feed": {
        "name": "ml_feed", "type": "ml",
        "owner": {"email": "ml@example.com"},
        "depends_on": {"nodes": ["model.p.m1", "model.p.m2"]}},
}


def _proj(tmp_path):
    return make_project(
        tmp_path,
        nodes={"model.p.m1": _node("m1", "compiled/m1.sql"),
               "model.p.m2": _node("m2", "compiled/m2.sql")},
        catalog_nodes={"model.p.m1": _cat("main", "m1", {"revenue": "HUGEINT", "user_id": "INT", "dt": "DATE"}),
                       "model.p.m2": _cat("main", "m2", {"revenue": "HUGEINT"}),
                       "seed.p.a": _cat("main", "a", {"amount": "INT"})},
        sqls={"compiled/m1.sql": "select sum(a.amount) as revenue, 1 as user_id, current_date as dt from main.a a",
              "compiled/m2.sql": "select sum(a.amount) as revenue from main.a a"},
        exposures=EXPO)


class TestExposureParsing:
    def test_deps_filtered_to_internal_models(self, tmp_path):
        p = _proj(tmp_path)
        ex = p.exposures
        assert ex["weekly_board"]["models"] == ["model.p.m1"]   # source 依赖不入
        assert ex["weekly_board"]["owner"] == {"name": "数据团队"}
        assert ex["weekly_board"]["label"] == "大盘周报"
        assert ex["ml_feed"]["label"] == "ml_feed"              # 无 label 回落 name
        assert ex["ml_feed"]["models"] == ["model.p.m1", "model.p.m2"]

    def test_graph_reverse_map(self, tmp_path):
        g = build_graph(_proj(tmp_path))
        by = g["exposures_by_model"]
        assert [e["name"] for e in by["model.p.m1"]] == ["ml_feed", "weekly_board"]
        assert [e["name"] for e in by["model.p.m2"]] == ["ml_feed"]


class TestCardConsumers:
    def test_target_exposures_dedup(self, tmp_path):
        g = build_graph(_proj(tmp_path))
        got = target_exposures(g, ["model.p.m1", "model.p.m2"])
        assert [e["name"] for e in got] == ["ml_feed", "weekly_board"]

    def test_no_exposures_empty(self, tmp_path):
        g = build_graph(_proj(tmp_path))
        assert target_exposures(g, ["model.p.nope"]) == []


class TestGovernanceWeighting:
    def test_pair_carries_consumer_names(self, tmp_path):
        r = scan(build_graph(_proj(tmp_path)), MLConfig(metrics=[1]))
        assert len(r["duplicates"]) == 1
        pair = r["duplicates"][0]
        # m1 喂两个下游,m2 喂一个——收敛方向可读
        sides = {pair["a"].split(".")[0]: pair.get("exposures_a"),
                 pair["b"].split(".")[0]: pair.get("exposures_b")}
        assert sides["m1"] == ["ml_feed", "weekly_board"]
        assert sides["m2"] == ["ml_feed"]


class TestDriftTargeting:
    def test_events_annotated(self, tmp_path):
        g = build_graph(_proj(tmp_path))
        cfg = SimpleNamespace(metrics=[
            SimpleNamespace(key="rev", target="m1.revenue", extra_targets=[])])
        events = [{"metric_key": "rev", "kind": "condition_added",
                   "severity": "high", "detail": {}},
                  {"metric_key": "ghost", "kind": "metric_added",
                   "severity": "info", "detail": {}}]
        annotate_exposures(events, cfg, g)
        assert events[0]["exposures"] == ["ml_feed", "weekly_board"]
        assert "exposures" not in events[1]           # 未知指标不阻断不误标


class TestInitCandidates:
    def test_numeric_columns_prefilled(self, tmp_path):
        block = _exposure_candidates(_proj(tmp_path))
        assert "weekly_board" in block and "dashboard" in block
        assert "#     target: m1.revenue" in block
        assert "user_id" not in block                 # id 类列不进候选
        assert "dt" not in block.replace("dbt", "")   # 非数值列不进候选

    def test_no_exposures_no_block(self, tmp_path):
        p = make_project(
            tmp_path,
            nodes={"model.p.m1": _node("m1", "compiled/m1.sql")},
            catalog_nodes={"model.p.m1": _cat("main", "m1", {"x": "INT"}),
                           "seed.p.a": _cat("main", "a", {"amount": "INT"})},
            sqls={"compiled/m1.sql": "select sum(a.amount) as x from main.a a"})
        assert _exposure_candidates(p) == ""
