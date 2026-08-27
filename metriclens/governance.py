#!/usr/bin/env python3
"""指标指纹重复扫描(治理引擎的确定性部分)。

指纹 = hash(排序后的源字段集 + 排序后的归一化业务条件指纹集)。
跨表同指纹、且两列互不为对方的血缘上下游 → 按确定性证据分档:
  聚合签名不同        → 同源不同义直判(计数 vs 求和/去重 vs 不去重),零 LLM;
  输出粒度(grain)不同 → 同指标家族的不同粒度物化,单独分档,不算重复建设;
  列基名一致          → A 档直判重复;
  其余                → B 档留 LLM 表达式链仲裁(arbitrate)。
"""
import hashlib
import json

import sqlglot
from sqlglot import exp

from metriclens.config import MLConfig
from metriclens.lineage import dialect
from metriclens.trace import trace


def fingerprint_of(t: dict) -> str:
    src = sorted(f"{s['table']}.{s['column']}" for s in t["sources"])
    conds = sorted({c["fp"] for c in t["conditions"] if not c.get("is_pure_key")})
    return hashlib.md5(json.dumps([src, conds]).encode()).hexdigest()[:16]


def agg_signature(t: dict) -> tuple:
    """表达式链上的聚合语义签名:函数名 + 是否 DISTINCT。签名不同的两列必非重复物化。"""
    sigs = set()
    for e in t["expr_chain"]:
        if not e.get("expr"):
            continue
        try:
            node = sqlglot.parse_one(e["expr"], read=dialect())
        except Exception:
            continue
        for f in node.find_all(exp.AggFunc):
            name = type(f).__name__.lower()
            sigs.add(name + (":distinct" if f.find(exp.Distinct) else ""))
    return tuple(sorted(sigs))


def base(col: str, suffixes: list) -> str:
    """列基名:剥离物化后缀,用于 A 档同名判定与卡片治理提示。"""
    for suf in suffixes:
        col = col.removesuffix(suf)
    return col


def scan(graph: dict, cfg: MLConfig) -> dict:
    layers = set(cfg.scan_layers)
    skip_cols = set(cfg.skip_columns)
    skip_suf = tuple(cfg.skip_suffixes)
    groups, chains, aggs, grains = {}, {}, {}, {}
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
            aggs[(name, col)] = agg_signature(t)
            # 无 group-by 的直通模型(join 取数)自身 grain 为空:沿值链继承最近聚合层的粒度
            grains[(name, col)] = next(
                (tuple(graph["models"][e["model"]].get("grain") or [])
                 for e in t["expr_chain"] if graph["models"][e["model"]].get("grain")), ())
    dup_pairs, cand_pairs, families, agg_distinct = [], [], [], []
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
                # 签名为空 = 证据不可见(聚合藏在模型内 CTE / 全链无分组),不可比:
                # 只在两侧证据都在场时下确定性结论,缺证一侧一律落到下一级判据
                if aggs[a] and aggs[b] and aggs[a] != aggs[b]:
                    agg_distinct.append({**pair, "agg_a": list(aggs[a]), "agg_b": list(aggs[b])})
                    continue          # 聚合语义不同 → 确定性不同义,无须 LLM
                if grains[a] and grains[b] and grains[a] != grains[b]:
                    families.append({**pair, "grain_a": list(grains[a]), "grain_b": list(grains[b])})
                    continue          # 同指标家族的不同粒度物化,单独分档
                same_base = base(a[1], cfg.base_suffixes) == base(b[1], cfg.base_suffixes)
                (dup_pairs if same_base else cand_pairs).append(pair)
    return {"duplicates": dup_pairs, "candidates": cand_pairs,
            "families": families, "agg_distinct": agg_distinct}
