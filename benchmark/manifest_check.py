#!/usr/bin/env python3
"""骨架校验:字段级血缘图投影到表级后,必须与 dbt manifest.json 的表级 DAG 一致。

投影用两条独立路径:①列级边的上游表并集;②重新解析编译 SQL 收集全部表引用。
两者与 manifest 三方对拍,不一致即告警。
"""
import json
import sys

import sqlglot
from sqlglot import exp

from benchmark.paths import GRAPH, PROJECT_DIR
from fineprint.tracing import load_graph


def main():
    mf = json.load(open(PROJECT_DIR / "target" / "manifest.json"))
    graph = load_graph(GRAPH)
    dialect = graph["meta"]["dialect"]
    expected = {}
    for nid, node in mf["nodes"].items():
        if not nid.startswith("model."):
            continue
        expected[node["name"]] = {d.split(".")[-1] for d in node["depends_on"]["nodes"]}

    ok = True
    for uid, m in graph["models"].items():
        name = m.get("name") or uid
        proj_cols = set()
        for c in m["columns"].values():
            for u in c.get("upstreams", []):
                proj_cols.add(u["table"].split(".")[-1])   # 物理三段键取裸表名对拍
        ast = sqlglot.parse_one((PROJECT_DIR / m["compiled_path"]).read_text(), read=dialect)
        cte_names = {c.alias for c in ast.find_all(exp.CTE)}
        proj_sql = {t.name for t in ast.find_all(exp.Table) if t.name not in cte_names and t.name != name}
        exp_deps = expected.get(name, set())
        miss_cols = exp_deps - proj_cols
        extra_cols = proj_cols - exp_deps
        miss_sql = exp_deps - proj_sql
        extra_sql = proj_sql - exp_deps
        status = "✓" if not (miss_sql or extra_sql) else "✗"
        note = ""
        if miss_cols:
            note += f"  列级投影未覆盖(仅条件引用): {sorted(miss_cols)}"
        if extra_cols or miss_sql or extra_sql:
            note += f"  异常: extra_col={sorted(extra_cols)} miss_sql={sorted(miss_sql)} extra_sql={sorted(extra_sql)}"
            ok = False if (miss_sql or extra_sql or extra_cols) else ok
        print(f"  {status} {name:<28} manifest={len(exp_deps)} 列级投影={len(proj_cols)} SQL投影={len(proj_sql)}{note}")
    print("\nmanifest 骨架校验:", "全部一致 ✓" if ok else "存在不一致 ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
