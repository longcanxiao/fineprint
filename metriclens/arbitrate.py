#!/usr/bin/env python3
"""治理报告:指纹扫描(A/B 两档)+ B 档 LLM 语义仲裁。

A 档(同源同条件同基名)直判重复,零 LLM;
B 档(同源同条件但列名不同)指纹无法区分——判据在表达式:
两列的表达式链交给 LLM 判"同一业务语义的重复物化"还是"同源的不同指标"。
"""
import json
from datetime import datetime
from pathlib import Path

from metriclens import prompts
from metriclens.config import MLConfig
from metriclens.governance import scan
from metriclens.llm import chat_json, fast_model, set_cache_dir
from metriclens.project import DbtProject
from metriclens.trace import trace


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


def arbitrate_pair(lang: str, graph: dict, pair: dict, docs: dict) -> dict:
    user = ("两列指纹一致(同源字段集 + 同业务条件集),表达式链如下:\n\n"
            f"列 A:\n{json.dumps(_expr_brief(graph, pair['a'], docs), ensure_ascii=False, indent=1)}\n\n"
            f"列 B:\n{json.dumps(_expr_brief(graph, pair['b'], docs), ensure_ascii=False, indent=1)}")
    return chat_json(prompts.ARB[lang], user, max_tokens=2000, model=fast_model(), validator=validate_arb)


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
    for p in r["candidates"]:
        arb = arbitrate_pair(cfg.language, graph, p, docs)
        item = {**p, "tier": "B", **arb}
        (duplicates if arb["verdict"] == "duplicate" else distinct).append(item)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "llm_model": fast_model(),
        "a_tier_pairs": len(r["duplicates"]), "b_tier_pairs": len(r["candidates"]),
        "duplicates": duplicates, "distinct": distinct,
    }
    f = report_path(project)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    tmp.replace(f)
    return report


def print_report(report: dict):
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
