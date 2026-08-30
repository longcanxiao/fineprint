# -*- coding: utf-8 -*-
"""质量门禁核心测试:归一化判等、LLM 结构校验、空引用拒绝、批次发布、漂移对比、治理 API 契约。"""
import json
import sys
from pathlib import Path

import pytest
import sqlglot

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint.lineage import fingerprint, normalize_condition  # noqa: E402

# benchmark 数仓产物(不入库):本地 jobs/rebuild.sh 生成;CI 上跳过依赖它们的用例
DB_EXISTS = (ROOT / "warehouse" / "metriclens.duckdb").exists()
GRAPH_EXISTS = (ROOT / "warehouse" / "dbt_project" / ".fineprint" / "graph.json").exists()


def _norm(sql):
    return normalize_condition(sqlglot.parse_one(sql, read="duckdb"))


class TestNormalize:
    def test_literal_side_swap(self):
        assert _norm("a <= 14") == _norm("14 >= a")

    def test_alias_stripped(self):
        assert _norm("o.order_status in (20, 30)") == _norm("x.order_status in (20, 30)")

    def test_different_conditions_differ(self):
        assert fingerprint(_norm("a <= 14")) != fingerprint(_norm("a <= 15"))


class TestLLMValidators:
    def test_hop_rejects_missing_columns(self):
        from fineprint.synth import validate_hop
        with pytest.raises(ValueError):
            validate_hop({"filters": []})

    def test_hop_rejects_bad_filter(self):
        from fineprint.synth import validate_hop
        with pytest.raises(ValueError):
            validate_hop({"columns": {}, "filters": [{"kind": "where"}]})  # 缺 quote

    def test_merge_requires_formula(self):
        from fineprint.synth import validate_merge
        with pytest.raises(ValueError):
            validate_merge({"summary": "x"})

    def test_biz_requires_evidence_ids(self):
        from fineprint.synth import validate_biz
        with pytest.raises(ValueError):
            validate_biz({"definition": "x", "clauses": [{"text": "t", "basis": "b"}]})  # 缺 evidence_ids


class TestVerifyQuotes:
    """空引用/幻觉引用拒绝——直接调用生产函数 verify_quotes(非复刻逻辑)。"""

    def test_empty_quote_rejected(self):
        from fineprint.synth import verify_quotes
        out = verify_quotes({"filters": [{"quote": "", "kind": "where"}]}, "select 1 from t where a=1")
        assert out["filters"][0]["quote_verified"] is False

    def test_fabricated_quote_rejected(self):
        from fineprint.synth import verify_quotes
        out = verify_quotes({"filters": [{"quote": "b = 2", "kind": "where"}]}, "select 1 from t where a=1")
        assert out["filters"][0]["quote_verified"] is False

    def test_real_quote_passes(self):
        from fineprint.synth import verify_quotes
        out = verify_quotes({"filters": [{"quote": "where  A=1", "kind": "where"}]},
                            "select 1 from t where a=1")
        assert out["filters"][0]["quote_verified"] is True


class TestCrossValidateSeeds:
    """seed / 未声明 source 的项目(如 jaffle_shop):通道一叶子表须计入源表集,不得恒判 S 漏。"""

    def test_seed_leaf_table_counts_as_source(self):
        from fineprint.synth import cross_validate
        t = {"sources": [{"table": "raw_payments", "column": "amount"}],
             "conditions": [], "semantics": []}
        hops = {"stg_payments": {"columns": {"amount": {
            "source_columns": [{"table": '"jaffle"."main"."raw_payments"', "column": "amount"}]}},
            "filters": []}}
        cls = {"f1_fps": {}, "f2_fps": set(), "quote_fail": 0,
               "out_of_scope": [], "unparsed": [], "suspect": []}
        v = cross_validate(t, hops, cls, source_names=set())   # dbt sources 为空
        assert v["s_missing_by_llm"] == [] and v["confidence"] == "high"


class TestGovernanceScan:
    @pytest.mark.skipif(not GRAPH_EXISTS, reason="需要本地血缘图(jobs/rebuild.sh)")
    def test_t8_pair_auto_discovered(self):
        import subprocess
        r = subprocess.run([sys.executable, "-m", "benchmark.governance_scan_check"],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


class TestStorePublish:
    """批次发布原子性:active 指针只认已完整落盘的 run 目录。"""

    def test_activate_and_read_back(self, tmp_path):
        from fineprint.store import CaliberStore
        st = CaliberStore(tmp_path)
        assert st.active_dir() is None                     # 无指针 → 无 active
        st.run_dir("abc123")
        st.activate("abc123")
        assert st.active_run_id() == "abc123"
        assert st.active_dir() == tmp_path / "runs" / "abc123"

    def test_pointer_to_missing_dir_is_inactive(self, tmp_path):
        from fineprint.store import CaliberStore
        st = CaliberStore(tmp_path)
        st.activate("ghost")                               # 指针指向不存在的目录
        assert st.active_dir() is None

    def test_prune_keeps_recent(self, tmp_path):
        import os
        import time
        from fineprint.store import CaliberStore
        st = CaliberStore(tmp_path)
        for i, rid in enumerate(["r1", "r2", "r3", "r4"]):
            d = st.run_dir(rid)
            os.utime(d, (time.time() + i, time.time() + i))
        st.prune(keep=2, protect="r4")
        left = {d.name for d in st.runs.iterdir()}
        assert "r4" in left and "r3" in left and "r1" not in left


class TestDriftDiff:
    """漂移对比:合成快照验证事件种类与严重度。"""

    @staticmethod
    def _snap(**over):
        base = {"target": "m.c", "sources": ["src_a.x"],
                "conditions": {"fp1": {"sql": "a = 1", "kind": "where", "model": "m"}},
                "semantics": [["case_when", "m", "case when d <= 14 then x end"]],
                "exprs": {"m.c": "sum(x)"}}
        base.update(over)
        return base

    def test_identical_no_events(self):
        from fineprint.drift import diff_metric
        assert diff_metric("k", self._snap(), self._snap()) == []

    def test_condition_change_high(self):
        from fineprint.drift import diff_metric
        new = self._snap(conditions={"fp2": {"sql": "a = 2", "kind": "where", "model": "m"}})
        ev = diff_metric("k", self._snap(), new)
        kinds = {(e["kind"], e["severity"]) for e in ev}
        assert ("condition_removed", "high") in kinds and ("condition_added", "high") in kinds

    def test_source_removed_high(self):
        from fineprint.drift import diff_metric
        ev = diff_metric("k", self._snap(), self._snap(sources=[]))
        assert [(e["kind"], e["severity"]) for e in ev] == [("source_removed", "high")]

    def test_semantic_change_high(self):
        from fineprint.drift import diff_metric
        new = self._snap(semantics=[["case_when", "m", "case when d <= 15 then x end"]])
        ev = diff_metric("k", self._snap(), new)
        assert {e["severity"] for e in ev} == {"high"}
        assert {e["kind"] for e in ev} == {"semantic_removed", "semantic_added"}

    def test_expr_changed_medium(self):
        from fineprint.drift import diff_metric
        ev = diff_metric("k", self._snap(), self._snap(exprs={"m.c": "sum(y)"}))
        assert [(e["kind"], e["severity"]) for e in ev] == [("expr_changed", "medium")]


class TestArbitrate:
    def test_validator_rejects_bad_verdict(self):
        from fineprint.arbitrate import validate_arb
        with pytest.raises(ValueError):
            validate_arb({"verdict": "maybe", "reason": "x"})

    def test_validator_accepts_good(self):
        from fineprint.arbitrate import validate_arb
        validate_arb({"verdict": "distinct", "reason": "计数 vs 比率"})


class TestConfig:
    def test_load_requires_metrics(self, tmp_path):
        from fineprint.config import MLConfig
        (tmp_path / "fineprint.yml").write_text("language: zh\nmetrics: []\n")
        with pytest.raises(ValueError):
            MLConfig.load(tmp_path)

    def test_load_roundtrip(self, tmp_path):
        from fineprint.config import MLConfig
        (tmp_path / "fineprint.yml").write_text(
            "language: en\nmetrics:\n  - key: gmv\n    title: GMV\n    target: m.gmv\n")
        cfg = MLConfig.load(tmp_path)
        assert cfg.language == "en" and cfg.metric("gmv").target == "m.gmv"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from server.main import app
    return TestClient(app)


class TestAPIContract:
    def test_reversed_dates_422(self, client):
        assert client.get("/api/overview?start=2026-08-24&end=2026-07-01").status_code == 422

    def test_bad_dim_422(self, client):
        assert client.get("/api/breakdown?start=2026-08-01&end=2026-08-10&dim=hack").status_code == 422

    def test_bad_date_format_422(self, client):
        assert client.get("/api/trend?start=notadate&end=2026-08-10").status_code == 422

    @pytest.mark.skipif(not DB_EXISTS, reason="需要本地 benchmark 数仓(jobs/rebuild.sh)")
    def test_share_denominator_is_global(self, client):
        r = client.get("/api/breakdown?start=2026-07-26&end=2026-08-24&dim=live_room").json()
        top_share = sum(row["share"] for row in r["rows"])
        assert top_share < 0.99          # Top30 份额合计必须小于全局(此前 bug 恒为 1.0)
        assert abs(sum(row["gmv"] for row in r["rows"]) / r["grand_total_gmv"] - top_share) < 1e-6


class TestCaliberAPI:
    """口径 API 只读 active 批次;review 卡对外裁剪为状态占位。"""

    def _store_with(self, tmp_path, card):
        from fineprint.store import CaliberStore
        st = CaliberStore(tmp_path)
        d = st.run_dir("r1")
        (d / f"{card['metric_key']}.json").write_text(json.dumps(card))
        st.activate("r1")
        return st

    def test_review_card_redacted(self, client, tmp_path, monkeypatch):
        import server.main as sm
        st = self._store_with(tmp_path, {
            "metric_key": "x", "title": "测试", "confidence": "low", "status": "review",
            "run_id": "r1", "generated_at": "2026-08-25T00:00:00",
            "technical": {"formula": "secret"}, "business": {"definition": "secret"}})
        monkeypatch.setattr(sm, "_store", st)
        r = client.get("/api/caliber/x").json()
        assert r["status"] == "review" and "message" in r
        assert "technical" not in r and "business" not in r   # 内容不得外泄

    def test_published_card_full(self, client, tmp_path, monkeypatch):
        import server.main as sm
        st = self._store_with(tmp_path, {
            "metric_key": "y", "title": "T", "confidence": "high", "status": "published",
            "run_id": "r1", "generated_at": "2026-08-25T00:00:00",
            "technical": {"formula": "f"}, "business": {"definition": "d"}})
        monkeypatch.setattr(sm, "_store", st)
        r = client.get("/api/caliber/y").json()
        assert r["technical"]["formula"] == "f"


class TestGovernanceAPI:
    def test_drift_filter_by_metric(self, client, tmp_path, monkeypatch):
        import server.main as sm
        (tmp_path / "drift_log.json").write_text(json.dumps({"events": [
            {"detected_at": "2026-08-26T10:00:00", "metric_key": "gmv", "kind": "expr_changed",
             "severity": "medium", "detail": {}},
            {"detected_at": "2026-08-26T10:00:00", "metric_key": "atv", "kind": "condition_added",
             "severity": "high", "detail": {"sql": "a = 1"}},
        ]}))
        monkeypatch.setattr(sm, "GOV_STORE", tmp_path)
        r = client.get("/api/governance/drift?metric_key=atv").json()
        assert r["total"] == 1 and r["events"][0]["kind"] == "condition_added"

    def test_report_missing_returns_empty(self, client, tmp_path, monkeypatch):
        import server.main as sm
        monkeypatch.setattr(sm, "GOV_STORE", tmp_path)
        r = client.get("/api/governance/report").json()
        assert r["duplicates"] == [] and "note" in r
