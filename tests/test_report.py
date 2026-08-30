# -*- coding: utf-8 -*-
"""HTML 报告渲染纪律:publication_status 是唯一门面状态,公式按 authority 展示,
证据编号可点击跳转到原文行;旧批次卡诚实回退旧视图。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint.report import card_html  # noqa: E402


def _card(**over):
    c = {
        "metric_key": "gmv", "title": "GMV", "target": "dm.gmv", "run_id": "r1",
        "status": "published", "confidence": "high", "generated_at": "t", "llm_model": "m",
        "graph_md5": "abcdef1234567890",
        "publication_status": "VERIFIED",
        "business": {"definition": "支付成功口径的成交额", "caveats": ["剔除测试账号"],
                     "clauses": [{"text": "只算支付成功", "evidence_ids": ["E1"]}]},
        "technical": {"formula": "sum(pay_amt)", "window": None},
        "technical_facts": {
            "formula": {"status": "proven", "authority": "machine", "top": "gmv",
                        "defs": [{"name": "gmv", "model": "dm", "column": "gmv",
                                  "expr": "SUM(ods.pay_amt_cent) / 100", "grain": ["dt"],
                                  "kind": "agg", "join_context": True}],
                        "inline": "SUM(ods.pay_amt_cent) / 100", "reasons": [], "rt_failed": False},
            "key_filters": {"status": "proven", "items": [
                {"sql": "pay_status = 'SUCCESS'", "kind": "where", "model": "dwd",
                 "line": 5, "evidence": "E1"}]},
            "window": {"status": "proven", "items": [
                {"sql": "ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC)",
                 "idiom": "dedup", "model": "dwd", "line": 6}]},
            "grain": {"status": "proven", "keys": ["dt"], "model": "dm"},
            "sources": {"status": "proven", "items": []},
        },
        "race": {"verdict": "agree", "detail": {"note": ""}},
        "validation": {"f1_total": 3, "f1_covered": 1.0, "f2_suspect": [],
                       "unverified_clauses": 0},
        "evidence": [{"id": "E1", "kind": "condition:where", "model": "dwd",
                      "line": 5, "text": "pay_status = 'SUCCESS'"}],
    }
    c.update(over)
    return c


class TestAuthorityRendering:
    def test_machine_authority_shows_composer_formula(self):
        h = card_html(_card())
        assert "组合器(发布权威)" in h
        assert "SUM(ods.pay_amt_cent) / 100" in h            # 组合器公式是主展示
        assert "粒度" in h and "聚合跨 join" in h
        assert "解释与叙述,非发布口径" in h                    # LLM 公式退居注脚
        assert "pay_status = &#x27;SUCCESS&#x27;" in h        # 机器关键过滤进正文
        assert "窗口/dedup" in h and "输出粒度" in h

    def test_llm_fallback_labeled(self):
        c = _card()
        c["technical_facts"]["formula"].update(
            authority="llm_fallback", status="ambiguous",
            reasons=["多目标指标:组合关系由配置声明"], top=None, defs=[], inline=None)
        h = card_html(c)
        assert "LLM 兜底" in h and "组合器未覆盖" in h
        assert "多目标指标" in h and "sum(pay_amt)" in h      # 兜底时 LLM 公式为主并给机器原因

    def test_legacy_card_falls_back(self):
        c = _card()
        del c["publication_status"], c["technical_facts"], c["race"]
        h = card_html(c)
        assert 'c-high">high' in h                            # 旧卡回退置信徽标
        assert "sum(pay_amt)" in h and "发布权威" not in h


class TestPublicationStatus:
    def test_verified_chip_replaces_confidence(self):
        h = card_html(_card())
        assert "VERIFIED" in h
        assert 'c-high">high' not in h                        # 置信度不再冒充门面状态
        assert "叙述互验置信度 high" in h                      # 降级为互验注脚

    def test_technical_only_drafts_business(self):
        c = _card(publication_status="TECHNICAL_ONLY", confidence="medium",
                  validation={"f1_total": 3, "f1_covered": 1.0, "f2_suspect": [],
                              "unverified_clauses": 0,
                              "freetext_unverified": {"caveats[1]": ["14"]}})
        h = card_html(c)
        assert "TECHNICAL_ONLY · 叙述待审" in h
        assert "待审草稿" in h and "勿作口径依据" in h
        i_draft, i_def = h.index("待审草稿"), h.index("支付成功口径的成交额")
        assert i_draft < i_def                                # 叙述被折叠进草稿块
        assert "组合器(发布权威)" in h                         # 机器事实仍正式展示

    def test_review_required_only_summary(self):
        c = _card(publication_status="REVIEW_REQUIRED",
                  race={"verdict": "disagree", "detail": {"note": "公式结构冲突"}})
        c["technical_facts"]["formula"]["reasons"] = ["标量子查询"]
        h = card_html(c)
        assert "未通过发布门禁" in h and "disagree" in h and "公式结构冲突" in h
        assert "支付成功口径的成交额" not in h                 # 口径内容不进正文
        assert "SUM(ods.pay_amt_cent)" not in h
        assert "证据原文" in h                                 # 评审仍可溯源

    def test_review_queue_stub_unchanged(self):
        h = card_html(_card(status="review"))
        assert "审核中" in h and "支付成功" not in h


class TestTraceability:
    def test_badges_jump_to_evidence_rows(self):
        h = card_html(_card(), files={"dwd": "models/dwd/dwd.sql"})
        assert '<a class="ev" href="#ev-gmv-E1"' in h         # 条款徽标=锚点
        assert 'id="ev-gmv-E1"' in h                          # 证据行带同名锚
        assert "models/dwd/dwd.sql" in h and "编译行 L5" in h  # 文件+行号
        assert "图 abcdef123456" in h                          # 来源图 hash
