#!/usr/bin/env python3
"""治理种子:指标指纹重复扫描(M5 指纹引擎的第一块)。

指纹 = hash(排序后的 ODS 源字段集 + 排序后的归一化业务条件集)。
跨表同指纹、且两列互不为对方的血缘上下游 → 重复建设候选。
"""
import hashlib
import json
import sys

from lineage.trace import load_graph, trace

SKIP_SUFFIX = ("_id", "_date", "_time", "_path")
SKIP_NAMES = {"dt", "province", "category_name", "channel_id", "attributed_channel",
              "carrier", "warehouse_code", "nick_name", "gender", "register_channel",
              "currency", "refund_type", "refund_status", "refund_reason", "logistics_status",
              "order_status", "pay_type", "sku_name", "is_presale"}


def fingerprint_of(t: dict) -> str:
    src = sorted(f"{s['table']}.{s['column']}" for s in t["sources"])
    conds = sorted({c["fp"] for c in t["conditions"] if not c.get("is_pure_key")})
    return hashlib.md5(json.dumps([src, conds]).encode()).hexdigest()[:16]


def base(col: str) -> str:
    """列基名:剥离常见物化后缀,用于 A 档同名判定与卡片治理提示。"""
    for suf in ("_total", "_14d", "_1d"):
        col = col.removesuffix(suf)
    return col


def scan(graph=None):
    graph = graph or load_graph()
    groups = {}
    chains = {}
    for name, m in graph["models"].items():
        if m["layer"] not in ("dwm", "dm", "app"):
            continue
        for col in m["columns"]:
            if col in SKIP_NAMES or col.endswith(SKIP_SUFFIX):
                continue
            t = trace(graph, name, col)
            if not t["sources"]:
                continue
            fp = fingerprint_of(t)
            groups.setdefault(fp, []).append((name, col))
            chains[(name, col)] = {(e["model"], e["column"]) for e in t["expr_chain"]}
    dup_pairs, cand_pairs = [], []
    for fp, members in groups.items():
        tables = {m for m, _ in members}
        if len(members) < 2 or len(tables) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a[0] == b[0]:
                    continue
                if b in chains[a] or a in chains[b]:
                    continue          # 血缘直系是引用,不是重复
                pair = {"fingerprint": fp, "a": f"{a[0]}.{a[1]}", "b": f"{b[0]}.{b[1]}"}
                # A 档:基名一致 → 重复建设/多处物化(建议收敛);B 档:同源同条件但列名不同 → 留 LLM 语义仲裁(M5)
                (dup_pairs if base(a[1]) == base(b[1]) else cand_pairs).append(pair)
    return {"duplicates": dup_pairs, "candidates": cand_pairs}


def target_pair_found(result=None) -> bool:
    result = result or scan()
    target = {"dm_trade_stats_1d.refund_amt", "dm_after_sale_stats_1d.refund_amt_total"}
    return any({p["a"], p["b"]} == target for p in result["duplicates"])


def main():
    r = scan()
    print("=== 指标指纹重复扫描(dwm/dm/app 全列)===\n")
    print(f"A 档 · 重复建设/多处物化(基名一致,建议收敛,{len(r['duplicates'])} 对):")
    for p in r["duplicates"]:
        print(f"  ⚠ [{p['fingerprint']}] {p['a']}  ≡  {p['b']}")
    print(f"\nB 档 · 同源同条件候选(列名不同,表达式语义留 LLM 仲裁,{len(r['candidates'])} 对):")
    for p in r["candidates"]:
        print(f"  · [{p['fingerprint']}] {p['a']}  ~  {p['b']}")
    found = target_pair_found(r)
    print(f"\n  T8 靶向对(交易域/售后域退款金额)自动发现于 A 档: {'✓' if found else '✗'}")
    sys.exit(0 if found else 1)


if __name__ == "__main__":
    main()
