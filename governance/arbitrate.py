#!/usr/bin/env python3
"""M5 治理报告:指纹扫描(A/B 两档)+ B 档 LLM 语义仲裁。

A 档(同源同条件同基名)直接判重复,无需 LLM;
B 档(同源同条件但列名不同)指纹无法区分——判据在表达式:
把两列的表达式链交给 LLM 判"同一业务语义的重复物化"还是"同源的不同指标"。
"""
import json
from datetime import datetime

from caliber.llm import chat_json, fast_model
from caliber.pipeline import load_column_docs
from governance.snapshot import STORE
from lineage.governance_scan import scan
from lineage.trace import load_graph, trace

REPORT = STORE / "governance_report.json"

ARB_SYSTEM = """你是指标治理仲裁器。给你两个数仓列,它们的 ODS 源字段集与业务过滤条件集完全相同(指纹一致),
但列名不同。请依据两列的表达式链判断:它们是"同一业务语义的重复物化"(duplicate),
还是"同源数据上的不同指标"(distinct,如同一明细上的计数 vs 比率、分子 vs 分母)。
只输出 JSON:
{"verdict": "duplicate|distinct",
 "reason": "一句话判据(引用表达式差异或等价性)",
 "suggestion": "治理建议一句话(duplicate → 建议收敛到哪个出口;distinct → 说明二者各自语义)"}
只依据给定表达式与注释判断,不要臆造。"""


def validate_arb(obj: dict):
    if obj.get("verdict") not in ("duplicate", "distinct"):
        raise ValueError(f"verdict 非法: {obj.get('verdict')!r}")
    if not isinstance(obj.get("reason"), str) or not obj["reason"]:
        raise ValueError("reason 缺失")


def _expr_brief(graph: dict, ref: str, docs: dict) -> dict:
    model, col = ref.rsplit(".", 1)
    t = trace(graph, model, col)
    return {
        "column": ref,
        "layer": graph["models"][model]["layer"],
        "doc": docs.get(model, {}).get(col, ""),
        "expr_chain": [{"col": f"{e['model']}.{e['column']}", "expr": e.get("expr", "")}
                       for e in t["expr_chain"]],
    }


def arbitrate_pair(graph: dict, pair: dict, docs: dict) -> dict:
    user = ("两列指纹一致(同 ODS 源字段集 + 同业务条件集),表达式链如下:\n\n"
            f"列 A:\n{json.dumps(_expr_brief(graph, pair['a'], docs), ensure_ascii=False, indent=1)}\n\n"
            f"列 B:\n{json.dumps(_expr_brief(graph, pair['b'], docs), ensure_ascii=False, indent=1)}")
    return chat_json(ARB_SYSTEM, user, max_tokens=2000, model=fast_model(), validator=validate_arb)


def build_report(graph: dict | None = None) -> dict:
    graph = graph or load_graph()
    docs = load_column_docs()
    r = scan(graph)
    duplicates, distinct = [], []
    for p in r["duplicates"]:
        duplicates.append({**p, "tier": "A", "verdict": "duplicate",
                           "reason": "基名一致且指纹相同:同一指标在多处物化",
                           "suggestion": "收敛到血缘最上游的单一出口,下游改为直接引用"})
    for p in r["candidates"]:
        arb = arbitrate_pair(graph, p, docs)
        item = {**p, "tier": "B", **arb}
        (duplicates if arb["verdict"] == "duplicate" else distinct).append(item)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "llm_model": fast_model(),
        "a_tier_pairs": len(r["duplicates"]), "b_tier_pairs": len(r["candidates"]),
        "duplicates": duplicates, "distinct": distinct,
    }


def save_report(report: dict):
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = REPORT.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    tmp.replace(REPORT)


def main():
    report = build_report()
    save_report(report)
    print(f"=== 治理报告(A 档 {report['a_tier_pairs']} 对直判 + B 档 {report['b_tier_pairs']} 对 LLM 仲裁)===\n")
    print(f"重复建设 {len(report['duplicates'])} 对:")
    for p in report["duplicates"]:
        print(f"  ⚠ [{p['tier']}] {p['a']}  ≡  {p['b']}")
        if p["tier"] == "B":
            print(f"       判据: {p['reason'][:90]}")
    print(f"\n同源不同义 {len(report['distinct'])} 对(合理,不收敛):")
    for p in report["distinct"]:
        print(f"  ✓ [{p['tier']}] {p['a']}  ~  {p['b']}")
        print(f"       判据: {p['reason'][:90]}")
    print(f"\n报告已写入 {REPORT}")


if __name__ == "__main__":
    main()
