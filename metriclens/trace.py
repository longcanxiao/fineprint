#!/usr/bin/env python3
"""反向遍历:从任一模型列出发回溯至源表,收敛 S(源字段)/F(过滤条件)/E(表达式链) 三元组。"""
import json
from pathlib import Path

from metriclens.lineage import set_dialect


def load_graph(path: Path) -> dict:
    g = json.loads(Path(path).read_text())
    set_dialect(g.get("meta", {}).get("dialect", "duckdb"))
    return g


def trace(graph: dict, model: str, column: str) -> dict:
    models = graph["models"]
    model_rel = graph.get("relations", {}).get("models", {})
    source_rel = graph.get("relations", {}).get("sources", {})
    if model not in models:
        raise KeyError(f"unknown model: {model}")
    if column not in models[model]["columns"]:
        raise KeyError(f"unknown column: {model}.{column} (有效列: {list(models[model]['columns'])[:8]}...)")

    sources, chain, visited = [], [], set()
    visited_models = []
    model_scopes: dict[str, set] = {}

    def add_source(rel: str, col: str):
        tbl = source_rel.get(rel) or rel.split(".", 1)[-1]
        key = {"table": tbl, "column": col}
        if key not in sources:
            sources.append(key)

    def rec_rowset(m: str):
        """表级行集访问(COUNT(*) 类断链兜底):收该模型的行集条件并继续沿行集表回溯,
        不进表达式链;model_scopes 留空集 → 只有 row_level 条件生效。"""
        if ("*", m) in visited:
            return
        visited.add(("*", m))
        if m not in visited_models:
            visited_models.append(m)
        model_scopes.setdefault(m, set())
        for rel in models[m].get("row_set_tables") or []:
            up_model = model_rel.get(rel)
            if up_model and up_model in models:
                rec_rowset(up_model)
            else:
                add_source(rel, "*")

    def rec(m: str, c: str, depth: int):
        if (m, c) in visited:
            return
        visited.add((m, c))
        if m not in visited_models:
            visited_models.append(m)
        entry = models[m]["columns"][c]
        model_scopes.setdefault(m, set()).update(entry.get("scopes", ["main"]))
        chain.append({
            "depth": depth, "layer": models[m]["layer"], "model": m, "column": c,
            "expr": entry.get("expr"), "src_path": models[m]["src_path"],
        })
        for up in entry.get("upstreams", []):
            rel = up["table"]
            up_model = model_rel.get(rel)
            if up["column"] == "*":
                if up_model and up_model in models:
                    rec_rowset(up_model)
                else:
                    add_source(rel, "*")
            elif up_model and up_model in models:
                rec(up_model, up["column"], depth + 1)
            else:
                add_source(rel, up["column"])

    rec(model, column, 0)

    conds, sems, seen_fp = [], [], set()
    for m in visited_models:
        scopes = model_scopes.get(m, {"main"})
        for c in models[m]["conditions"]:
            # 行集条件(main/FROM/inner join 闭包内)对全列生效;其余按值路径 scope 过滤
            if not c.get("row_level") and c["scope"] not in scopes:
                continue
            if c["fp"] in seen_fp:
                continue
            seen_fp.add(c["fp"])
            conds.append({**c, "src_path": models[m]["src_path"]})
        for s in models[m]["semantics"]:
            if s.get("scope", "main") not in scopes:
                continue
            if s.get("column") and (m, s["column"]) not in visited and s["type"] != "stat_date_key":
                continue
            sems.append({**s, "src_path": models[m]["src_path"]})

    return {
        "target": f"{models[model]['layer']}.{model}.{column}",
        "depth": 1 + max((e["depth"] for e in chain), default=0),
        "models_visited": visited_models,
        "sources": sorted(sources, key=lambda x: (x["table"], x["column"])),
        "expr_chain": chain,
        "conditions": conds,
        "semantics": sems,
    }


def render(t: dict) -> str:
    L = []
    L.append(f"◎ 目标: {t['target']}   (链路 {t['depth']} 层,经过 {len(t['models_visited'])} 个模型)")
    L.append("\n── 表达式链 E ──")
    for e in t["expr_chain"]:
        pad = "  " * e["depth"]
        expr = (e["expr"] or "").replace('"', "")
        if len(expr) > 96:
            expr = expr[:96] + "…"
        L.append(f"{pad}[{e['layer']}] {e['model']}.{e['column']} = {expr}")
    L.append(f"\n── 源字段 S ({len(t['sources'])} 个) ──")
    for s in t["sources"]:
        L.append(f"  {s['table']}.{s['column']}")
    key_conds = [c for c in t["conditions"] if not c.get("is_pure_key")]
    pure = [c for c in t["conditions"] if c.get("is_pure_key")]
    L.append(f"\n── 过滤条件 F ({len(key_conds)} 条业务条件 + {len(pure)} 条纯关联键) ──")
    for c in key_conds:
        jt = f" join({c.get('join_table')})" if c["kind"] == "join_on" else ""
        L.append(f"  [{c['kind']}{jt}] {c['sql'].replace(chr(34), '')}"
                 f"\n      ↳ {c['src_path']} · {c['model']}({c['scope']}) · 编译行 L{c['line']}")
    L.append(f"\n── 结构语义点 ({len(t['semantics'])}) ──")
    for s in t["semantics"]:
        desc = {
            "window": lambda s: f"窗口/{s['idiom']}: {s['sql'].replace(chr(34), '')}"
                      + (f"  → 按 {','.join(s['partition_by'])} 取 {s['order_by']} 首行" if s["idiom"] == "dedup" else ""),
            "case_when": lambda s: f"CASE WHEN → {s.get('column')}: {s['sql'][:80].replace(chr(34), '')}",
            "coalesce": lambda s: f"COALESCE 兜底 → {s.get('column')}: {s['sql'][:80].replace(chr(34), '')}",
            "stat_date_key": lambda s: f"统计日归属 → {s.get('column')} = {s['sql'].replace(chr(34), '')}",
        }[s["type"]](s)
        L.append(f"  [{s['model']}] {desc}  @L{s['line']}")
    return "\n".join(L)
