#!/usr/bin/env python3
"""血缘验收:golden set 评测——源字段识别 P/R 与关键过滤条件召回,目标 ≥95%。"""
import re
import sys
from pathlib import Path

import yaml

from benchmark.paths import GRAPH
from fineprint.tracing import load_graph, trace


def mnorm(s: str) -> str:
    s = s.lower().replace('"', "").replace("date_diff", "datediff")
    s = re.sub(r"\b[a-z_][a-z0-9_]*\.", "", s)   # 去表/别名限定,便于无前缀模式匹配
    return re.sub(r"\s+", " ", s).strip()


def haystack(t: dict) -> str:
    parts = []
    for c in t["conditions"]:
        parts += [c["norm"], c["sql"]]
    for s in t["semantics"]:
        parts.append(s["sql"])
        parts.append(" ".join(s.get("partition_by", []) or []))
    for e in t["expr_chain"]:
        if e["expr"]:
            parts.append(e["expr"])
        parts.append(e["column"])
    return mnorm(" || ".join(p for p in parts if p))


def main():
    g = load_graph(GRAPH)
    spec = yaml.safe_load(open(Path(__file__).parent / "golden_set.yml"))
    tot_exp_src = tot_hit_src = tot_pred_src = tot_tp_src = 0
    tot_exp_cond = tot_hit_cond = 0
    fails = []
    print("=== 血缘 golden set 评测 ===\n")
    for tg in spec["targets"]:
        model, col = tg["target"].rsplit(".", 1)
        t = trace(g, model, col)
        pred = {f"{s['table']}.{s['column']}" for s in t["sources"]}
        expd = set(tg["sources"])
        hit = pred & expd
        hay = haystack(t)
        cond_hits = [p for p in tg.get("conds", []) if mnorm(p) in hay]
        cond_miss = [p for p in tg.get("conds", []) if mnorm(p) not in hay]
        absent_bad = [p for p in tg.get("absent", []) if mnorm(p) in hay]
        tot_exp_src += len(expd)
        tot_hit_src += len(hit)
        tot_pred_src += len(pred)
        tot_tp_src += len(hit)
        tot_exp_cond += len(tg.get("conds", []))
        tot_hit_cond += len(cond_hits)
        ok = not cond_miss and not absent_bad and hit == expd and pred == expd
        extra = pred - expd
        print(f"  {'✓' if ok else '△'} {tg['target']}  [{','.join(tg['traps'])}]  "
              f"源字段 {len(hit)}/{len(expd)}命中"
              + (f" 多识别:{sorted(extra)}" if extra else "")
              + (f" 漏:{sorted(expd - hit)}" if expd - hit else "")
              + f"  条件召回 {len(cond_hits)}/{len(tg.get('conds', []))}"
              + (f" 漏:{cond_miss}" if cond_miss else "")
              + (f" 违反负向断言:{absent_bad}" if absent_bad else ""))
        if not ok:
            fails.append(tg["target"])
    for pair in spec.get("pair_equal_sources", []):
        (m1, c1), (m2, c2) = [p.rsplit(".", 1) for p in pair]
        s1 = {f"{s['table']}.{s['column']}" for s in trace(g, m1, c1)["sources"]}
        s2 = {f"{s['table']}.{s['column']}" for s in trace(g, m2, c2)["sources"]}
        eq = s1 == s2
        print(f"  {'✓' if eq else '✗'} T8 重复建设可识别: {pair[0]} 与 {pair[1]} 源字段集合"
              f"{'一致' if eq else f'不一致 {s1} vs {s2}'}")
        if not eq:
            fails.append("pair:" + "/".join(pair))
    sr = tot_hit_src / tot_exp_src * 100
    sp = tot_tp_src / tot_pred_src * 100 if tot_pred_src else 0
    cr = tot_hit_cond / tot_exp_cond * 100
    print(f"\n  源字段识别: 召回 {sr:.1f}%  精确 {sp:.1f}%   关键条件召回: {cr:.1f}%   目标 ≥95%")
    passed = sr >= 95 and sp >= 95 and cr >= 95 and not fails
    print("  血缘验收:", "PASS ✅" if passed else f"FAIL ❌ {fails}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
