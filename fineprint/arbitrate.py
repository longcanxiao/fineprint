#!/usr/bin/env python3
"""治理报告:指纹扫描(A/B 两档)+ B 档 LLM 语义仲裁。

A 档(同源同条件同基名)直判重复,零 LLM;
B 档(同源同条件但列名不同)指纹无法区分——判据在表达式:
两列的表达式链交给 LLM 判"同一业务语义的重复物化"还是"同源的不同指标"。
"""
import json
from datetime import datetime
from pathlib import Path

from fineprint import prompts  # noqa: F401  (HOP/MERGE 等核心提示词仍在 prompts)
from fineprint.config import MLConfig
from fineprint.governance import scan
from fineprint.llm import chat_json, fast_model, set_cache_dir
from fineprint.project import DbtProject
from fineprint.tracing import resolve_model, trace


# 治理仲裁提示词与组件同居:公开发行版摘除本模块即一并缺席
ARB = {
"zh": """你是指标治理仲裁器。给你两个数仓列,它们的源字段集与业务过滤条件集完全相同(指纹一致),但列名不同。请依据两列的表达式链判断:它们是"同一业务语义的重复物化"(duplicate),还是"同源数据上的不同指标"(distinct,如同一明细上的计数 vs 比率、分子 vs 分母)。
只输出 JSON:
{"verdict": "duplicate|distinct",
 "reason": "一句话判据(引用表达式差异或等价性)",
 "suggestion": "治理建议一句话(duplicate → 建议收敛到哪个出口;distinct → 说明二者各自语义)"}
只依据给定表达式与注释判断,不要臆造。""",
"en": """You are a metric-governance arbitrator. Two warehouse columns share identical source-column sets and business-filter sets (same fingerprint) but different names. Judge from their expression chains whether they are a "duplicate materialization of the same business semantics" (duplicate) or "different metrics on the same source" (distinct — e.g. count vs ratio, numerator vs denominator).
JSON only:
{"verdict": "duplicate|distinct",
 "reason": "one-line criterion (cite the expression difference or equivalence)",
 "suggestion": "one-line governance advice (duplicate → which outlet to converge on; distinct → what each means)"}
Judge only from the given expressions and descriptions; invent nothing.""",
}

def validate_arb(obj: dict):
    if obj.get("verdict") not in ("duplicate", "distinct"):
        raise ValueError(f"verdict 非法: {obj.get('verdict')!r}")
    if not isinstance(obj.get("reason"), str) or not obj["reason"]:
        raise ValueError("reason 缺失")


def _expr_brief(graph: dict, ref: str, docs: dict) -> dict:
    model, col = ref.rsplit(".", 1)
    uid = resolve_model(graph, model)   # 报告两侧是展示名(短名或 pkg:name),归位到 uid
    t = trace(graph, uid, col)
    return {
        "column": ref,
        "layer": graph["models"][uid]["layer"],
        "doc": docs.get(graph["models"][uid].get("name") or model, {}).get(col, ""),
        "expr_chain": [{"col": f"{e['model']}.{e['column']}", "expr": e.get("expr", "")}
                       for e in t["expr_chain"]],
    }


def arbitrate_pair(lang: str, graph: dict, pair: dict, docs: dict) -> dict:
    user = ("两列指纹一致(同源字段集 + 同业务条件集),表达式链如下:\n\n"
            f"列 A:\n{json.dumps(_expr_brief(graph, pair['a'], docs), ensure_ascii=False, indent=1)}\n\n"
            f"列 B:\n{json.dumps(_expr_brief(graph, pair['b'], docs), ensure_ascii=False, indent=1)}")
    return chat_json(ARB[lang], user, max_tokens=2000, model=fast_model(), validator=validate_arb)


def report_path(project: DbtProject) -> Path:
    return project.workspace / "governance_report.json"


def build_report(project: DbtProject, cfg: MLConfig, graph: dict) -> dict:
    set_cache_dir(project.workspace / "cache")
    docs = project.column_docs
    r = scan(graph, cfg)
    duplicates, distinct = [], []
    for p in r["duplicates"]:
        duplicates.append({**p, "tier": "A", "verdict": "duplicate",
                           "reason": "基名一致且指纹相同:同一指标在多处物化",
                           "suggestion": "收敛到血缘最上游的单一出口,下游改为直接引用"})
    for p in r["agg_distinct"]:
        distinct.append({**p, "tier": "A", "verdict": "distinct",
                         "reason": f"聚合语义不同({p['agg_a']} vs {p['agg_b']}):同源上的不同指标"})
    # B 档逐对 LLM 仲裁有真实调用成本:按配置上限截断,截断量显式入报告
    cand = r["candidates"]
    skipped = max(0, len(cand) - cfg.max_llm_pairs)
    if skipped:
        print(f"B 档候选 {len(cand)} 对超出上限,仅仲裁前 {cfg.max_llm_pairs} 对"
              f"(governance.max_llm_pairs 可调)")
    for p in cand[:cfg.max_llm_pairs]:
        arb = arbitrate_pair(cfg.language, graph, p, docs)
        item = {**p, "tier": "B", **arb}
        (duplicates if arb["verdict"] == "duplicate" else distinct).append(item)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        # 报告绑定生成它的图:验收据此检出"图已重建、报告仍是旧图产物"的混版本状态
        "graph_md5": graph.get("meta", {}).get("graph_md5"),
        "graph_generated_at": graph.get("meta", {}).get("generated_at"),
        # 零 B 档候选时报告纯确定性,不要求 LLM 凭据(质量立项/家族分档照常产出)
        "llm_model": fast_model() if cand[:cfg.max_llm_pairs] else None,
        # A 档直判 = 基名重复 + 聚合语义不同义两类;都是零 LLM 的确定性结论
        "a_tier_pairs": len(r["duplicates"]) + len(r["agg_distinct"]),
        "a_tier_dup": len(r["duplicates"]), "a_tier_agg_distinct": len(r["agg_distinct"]),
        "b_tier_pairs": len(cand) - skipped, "b_tier_skipped": skipped,
        "pairs_truncated": r.get("pairs_truncated", 0),
        "duplicates": duplicates, "distinct": distinct,
        "families": r["families"], "row_mismatch": r.get("row_mismatch", []),
        "sql_quality": r.get("sql_quality", []),
    }
    f = report_path(project)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)
    return report


def print_report(report: dict):
    skipped = report.get("b_tier_skipped", 0)
    print(f"=== 治理报告(A 档 {report['a_tier_pairs']} 对直判 + B 档 {report['b_tier_pairs']} 对 LLM 仲裁"
          + (f",另 {skipped} 对超出 max_llm_pairs 未仲裁" if skipped else "") + ")===\n")
    print(f"重复建设 {len(report['duplicates'])} 对:")
    for p in report["duplicates"]:
        print(f"  ⚠ [{p['tier']}] {p['a']}  ≡  {p['b']}")
        if p["tier"] == "B":
            print(f"       判据: {p['reason'][:90]}")
    print(f"\n同源不同义 {len(report['distinct'])} 对(合理,不收敛):")
    for p in report["distinct"]:
        print(f"  ✓ [{p['tier']}] {p['a']}  ~  {p['b']}")
        print(f"       判据: {p['reason'][:90]}")
    fams = report.get("families") or []
    if fams:
        print(f"\n同指标家族·不同粒度 {len(fams)} 对(非重复,建议统一命名口径):")
        for p in fams:
            print(f"  ◇ {p['a']}({','.join(p['grain_a']) or '明细'})  ~  {p['b']}({','.join(p['grain_b']) or '明细'})")
    rms = report.get("row_mismatch") or []
    if rms:
        print(f"\n同源同条件·行集结构不同 {len(rms)} 对(join 基数未证,须人工确认唯一性):")
        for p in rms:
            only = (p.get("rowset_only_a") or []) + (p.get("rowset_only_b") or [])
            print(f"  ⋈ {p['a']}  ?  {p['b']}   差异表: {', '.join(only[:4])}")
    sq = report.get("sql_quality") or []
    if sq:
        print(f"\nSQL 质量立项 {len(sq)} 项(口径含义未自证,须人工明确计数对象):")
        for q in sq:
            print(f"  ✗ {q['model']}.{q['column']} @L{q.get('line')}  行集 {' ⋈ '.join(q['tables'])}")
            print(f"       {q['reason']}")
    if report.get("b_tier_skipped"):
        print(f"\n(B 档截断 {report['b_tier_skipped']} 对未仲裁,governance.max_llm_pairs 可调)")
