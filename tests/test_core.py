# -*- coding: utf-8 -*-
"""质量门禁核心测试:归一化判等、LLM 结构校验、空引用拒绝、治理扫描、API 契约。"""
import sys
from pathlib import Path

import pytest
import sqlglot

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lineage.core import normalize_condition, fingerprint  # noqa: E402


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
        from caliber.pipeline import validate_hop
        with pytest.raises(ValueError):
            validate_hop({"filters": []})

    def test_hop_rejects_bad_filter(self):
        from caliber.pipeline import validate_hop
        with pytest.raises(ValueError):
            validate_hop({"columns": {}, "filters": [{"kind": "where"}]})  # 缺 quote

    def test_merge_requires_formula(self):
        from caliber.pipeline import validate_merge
        with pytest.raises(ValueError):
            validate_merge({"summary": "x"})

    def test_biz_requires_evidence_ids(self):
        from caliber.pipeline import validate_biz
        with pytest.raises(ValueError):
            validate_biz({"definition": "x", "clauses": [{"text": "t", "basis": "b"}]})  # 缺 evidence_ids


class TestVerifyQuotes:
    """空引用/幻觉引用拒绝——直接调用生产函数 verify_quotes(非复刻逻辑)。"""

    def test_empty_quote_rejected(self):
        from caliber.pipeline import verify_quotes
        out = verify_quotes({"filters": [{"quote": "", "kind": "where"}]}, "select 1 from t where a=1")
        assert out["filters"][0]["quote_verified"] is False

    def test_fabricated_quote_rejected(self):
        from caliber.pipeline import verify_quotes
        out = verify_quotes({"filters": [{"quote": "b = 2", "kind": "where"}]}, "select 1 from t where a=1")
        assert out["filters"][0]["quote_verified"] is False

    def test_real_quote_passes(self):
        from caliber.pipeline import verify_quotes
        out = verify_quotes({"filters": [{"quote": "where  A=1", "kind": "where"}]},
                            "select 1 from t where a=1")
        assert out["filters"][0]["quote_verified"] is True


class TestGovernanceScan:
    def test_t8_pair_auto_discovered(self):
        from lineage.governance_scan import target_pair_found
        assert target_pair_found()


class TestStorePublish:
    """批次发布原子性:active 指针只认已完整落盘的 run 目录。"""

    def test_activate_and_read_back(self, tmp_path, monkeypatch):
        import caliber.store_paths as sp
        monkeypatch.setattr(sp, "STORE", tmp_path)
        monkeypatch.setattr(sp, "RUNS", tmp_path / "runs")
        monkeypatch.setattr(sp, "POINTER", tmp_path / "active_run")
        assert sp.active_dir() is None                     # 无指针 → 无 active
        (tmp_path / "runs" / "abc123").mkdir(parents=True)
        sp.activate("abc123")
        assert sp.active_run_id() == "abc123"
        assert sp.active_dir() == tmp_path / "runs" / "abc123"

    def test_pointer_to_missing_dir_is_inactive(self, tmp_path, monkeypatch):
        import caliber.store_paths as sp
        monkeypatch.setattr(sp, "RUNS", tmp_path / "runs")
        monkeypatch.setattr(sp, "POINTER", tmp_path / "active_run")
        (tmp_path / "runs").mkdir()
        sp.activate("ghost")                               # 指针指向不存在的目录
        assert sp.active_dir() is None


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

    def test_share_denominator_is_global(self, client):
        r = client.get("/api/breakdown?start=2026-07-26&end=2026-08-24&dim=live_room").json()
        top_share = sum(row["share"] for row in r["rows"])
        assert top_share < 0.99          # Top30 份额合计必须小于全局(此前 bug 恒为 1.0)
        assert abs(sum(row["gmv"] for row in r["rows"]) / r["grand_total_gmv"] - top_share) < 1e-6


class TestCaliberAPI:
    """口径 API 只读 active 批次;review 卡对外裁剪为状态占位。"""

    def test_review_card_redacted(self, client, tmp_path, monkeypatch):
        import caliber.store_paths as sp
        run = tmp_path / "runs" / "r1"
        run.mkdir(parents=True)
        import json as _json
        (run / "x.json").write_text(_json.dumps({
            "metric_key": "x", "title": "测试", "confidence": "low", "status": "review",
            "run_id": "r1", "generated_at": "2026-08-25T00:00:00",
            "technical": {"formula": "secret"}, "business": {"definition": "secret"}}))
        monkeypatch.setattr(sp, "RUNS", tmp_path / "runs")
        monkeypatch.setattr(sp, "POINTER", tmp_path / "active_run")
        sp.activate("r1")
        r = client.get("/api/caliber/x").json()
        assert r["status"] == "review" and "message" in r
        assert "technical" not in r and "business" not in r   # 内容不得外泄

    def test_published_card_full(self, client, tmp_path, monkeypatch):
        import caliber.store_paths as sp
        run = tmp_path / "runs" / "r2"
        run.mkdir(parents=True)
        import json as _json
        (run / "y.json").write_text(_json.dumps({
            "metric_key": "y", "title": "T", "confidence": "high", "status": "published",
            "run_id": "r2", "generated_at": "2026-08-25T00:00:00",
            "technical": {"formula": "f"}, "business": {"definition": "d"}}))
        monkeypatch.setattr(sp, "RUNS", tmp_path / "runs")
        monkeypatch.setattr(sp, "POINTER", tmp_path / "active_run")
        sp.activate("r2")
        r = client.get("/api/caliber/y").json()
        assert r["technical"]["formula"] == "f"


class TestDriftDiff:
    """漂移对比:合成快照验证事件种类与严重度。"""

    @staticmethod
    def _snap(**over):
        base = {"target": "m.c", "sources": ["ods_a.x"],
                "conditions": {"fp1": {"sql": "a = 1", "kind": "where", "model": "m"}},
                "semantics": [["case_when", "m", "case when d <= 14 then x end"]],
                "exprs": {"m.c": "sum(x)"}}
        base.update(over)
        return base

    def test_identical_no_events(self):
        from governance.drift import diff_metric
        assert diff_metric("k", self._snap(), self._snap()) == []

    def test_condition_change_high(self):
        from governance.drift import diff_metric
        new = self._snap(conditions={"fp2": {"sql": "a = 2", "kind": "where", "model": "m"}})
        ev = diff_metric("k", self._snap(), new)
        kinds = {(e["kind"], e["severity"]) for e in ev}
        assert ("condition_removed", "high") in kinds and ("condition_added", "high") in kinds

    def test_source_removed_high(self):
        from governance.drift import diff_metric
        ev = diff_metric("k", self._snap(), self._snap(sources=[]))
        assert [(e["kind"], e["severity"]) for e in ev] == [("source_removed", "high")]

    def test_semantic_change_high(self):
        from governance.drift import diff_metric
        new = self._snap(semantics=[["case_when", "m", "case when d <= 15 then x end"]])
        ev = diff_metric("k", self._snap(), new)
        assert {e["severity"] for e in ev} == {"high"}
        assert {e["kind"] for e in ev} == {"semantic_removed", "semantic_added"}

    def test_expr_changed_medium(self):
        from governance.drift import diff_metric
        ev = diff_metric("k", self._snap(), self._snap(exprs={"m.c": "sum(y)"}))
        assert [(e["kind"], e["severity"]) for e in ev] == [("expr_changed", "medium")]


class TestArbitrate:
    def test_validator_rejects_bad_verdict(self):
        from governance.arbitrate import validate_arb
        with pytest.raises(ValueError):
            validate_arb({"verdict": "maybe", "reason": "x"})

    def test_validator_accepts_good(self):
        from governance.arbitrate import validate_arb
        validate_arb({"verdict": "distinct", "reason": "计数 vs 比率"})


class TestGovernanceAPI:
    def test_drift_filter_by_metric(self, client, tmp_path, monkeypatch):
        import json as _json
        import server.main as sm
        (tmp_path / "drift_log.json").write_text(_json.dumps({"events": [
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
