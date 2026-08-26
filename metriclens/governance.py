#!/usr/bin/env python3
"""指标指纹重复扫描(治理引擎的确定性部分)。

指纹 = hash(排序后的源字段集 + 排序后的归一化业务条件指纹集)。
跨表同指纹、且两列互不为对方的血缘上下游 → 重复建设候选:
A 档(基名一致)直判重复;B 档(列名不同)留 LLM 表达式链仲裁(arbitrate)。
"""
import hashlib
import json

from metriclens.config import MLConfig
from metriclens.trace import trace


def fingerprint_of(t: dict) -> str:
    src = sorted(f"{s['table']}.{s['column']}" for s in t["sources"])
    conds = sorted({c["fp"] for c in t["conditions"] if not c.get("is_pure_key")})
    return hashlib.md5(json.dumps([src, conds]).encode()).hexdigest()[:16]


def base(col: str, suffixes: list) -> str:
    """列基名:剥离物化后缀,用于 A 档同名判定与卡片治理提示。"""
    for suf in suffixes:
        col = col.removesuffix(suf)
    return col


def scan(graph: dict, cfg: MLConfig) -> dict:
    layers = set(cfg.scan_layers)
    skip_cols = set(cfg.skip_columns)
    skip_suf = tuple(cfg.skip_suffixes)
    groups, chains = {}, {}
    for name, m in graph["models"].items():
        if layers and m["layer"] not in layers:
            continue
        for col in m["columns"]:
            if col in skip_cols or col.endswith(skip_suf):
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
                same_base = base(a[1], cfg.base_suffixes) == base(b[1], cfg.base_suffixes)
                (dup_pairs if same_base else cand_pairs).append(pair)
    return {"duplicates": dup_pairs, "candidates": cand_pairs}
