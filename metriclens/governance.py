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
from metriclens.lineage import agg_one as _agg_one
from metriclens.lineage import dialect
from metriclens.trace import display_name, trace


def fingerprint_of(t: dict) -> str:
    # 源字段用物理全名(db.schema.table):跨 schema/跨库同名源表是不同数据
    src = sorted(f"{s.get('database', '')}.{s.get('schema', '')}.{s['table']}.{s['column']}"
                 for s in t["sources"])
    conds = sorted({c["fp"] for c in t["conditions"] if not c.get("is_pure_key")})
    return hashlib.md5(json.dumps([src, conds]).encode()).hexdigest()[:16]



def agg_signature(t: dict) -> tuple:
    """表达式链上的聚合语义签名:函数名 + 是否 DISTINCT(行数等价类归一化)。
    签名不同的两列必非重复物化。"""
    sigs = set()
    for e in t["expr_chain"]:
        if not e.get("expr"):
            continue
        try:
            node = sqlglot.parse_one(e["expr"], read=dialect())
        except Exception:
            continue
        for f in node.find_all(exp.AggFunc):
            sigs.add(_agg_one(f))
    return tuple(sorted(sigs))


def agg_maybe_equivalent(a: tuple, b: tuple) -> bool:
    """签名不同但可能语义等价的已知展开形:AVG(x) ↔ SUM(x)/COUNT(x)。
    此类不得确定性判不同义,降入 B 档 LLM 仲裁。"""
    sa, sb = set(a), set(b)
    return (sa == {"avg"} and {"sum", "count"} <= sb) or (sb == {"avg"} and {"sum", "count"} <= sa)


def base(col: str, suffixes: list) -> str:
    """列基名:剥离物化后缀,用于 A 档同名判定与卡片治理提示。"""
    for suf in suffixes:
        col = col.removesuffix(suf)
    return col


def scan(graph: dict, cfg: MLConfig) -> dict:
    layers = set(cfg.scan_layers)
    skip_cols = set(cfg.skip_columns)
    skip_suf = tuple(cfg.skip_suffixes)
    groups, chains, aggs, grains, disp = {}, {}, {}, {}, {}
    for uid, m in graph["models"].items():
        if layers and m["layer"] not in layers:
            continue
        disp[uid] = display_name(graph, uid)
        for col in m["columns"]:
            if col in skip_cols or col.endswith(skip_suf):
                continue
            t = trace(graph, uid, col)
            if not t["sources"]:
                continue
            fp = fingerprint_of(t)
            groups.setdefault(fp, []).append((uid, col))
            # 血缘直系判定用 uid(机器键);报告两侧输出展示名
            chains[(uid, col)] = {(e["model_uid"], e["column"]) for e in t["expr_chain"]}
            aggs[(uid, col)] = agg_signature(t)
            # 无 group-by 的直通模型(join 取数)自身 grain 为空:沿值链继承最近聚合层的粒度
            grains[(uid, col)] = next(
                (tuple(graph["models"][e["model_uid"]].get("grain") or [])
                 for e in t["expr_chain"] if graph["models"][e["model_uid"]].get("grain")), ())
    # 每类结对上限:同指纹组 O(n²) 在大项目可能爆炸,封顶防内存;截断量入报告。
    # 成员与指纹都排序——结果确定,不随 manifest 顺序漂移
    PAIR_CAP = max(2000, 50 * (cfg.max_llm_pairs or 40))
    dup_pairs, cand_pairs, families, agg_distinct = [], [], [], []
    truncated = 0
    for fp in sorted(groups):
        members = sorted(groups[fp])
        tables = {m for m, _ in members}
        if len(members) < 2 or len(tables) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if len(dup_pairs) + len(cand_pairs) + len(families) + len(agg_distinct) >= PAIR_CAP:
                    truncated += 1
                    continue
                a, b = members[i], members[j]
                if a[0] == b[0]:
                    continue
                if b in chains[a] or a in chains[b]:
                    continue          # 血缘直系是引用,不是重复
                pair = {"fingerprint": fp, "a": f"{disp[a[0]]}.{a[1]}", "b": f"{disp[b[0]]}.{b[1]}"}
                # 签名为空 = 证据不可见(聚合藏在模型内 CTE / 全链无分组),不可比:
                # 只在两侧证据都在场时下确定性结论,缺证一侧一律落到下一级判据;
                # 已知等价展开形(avg ↔ sum/count)不直判,降入 B 档仲裁
                if aggs[a] and aggs[b] and aggs[a] != aggs[b]:
                    if agg_maybe_equivalent(aggs[a], aggs[b]):
                        cand_pairs.append(pair)
                        continue
                    agg_distinct.append({**pair, "agg_a": list(aggs[a]), "agg_b": list(aggs[b])})
                    continue          # 聚合语义不同 → 确定性不同义,无须 LLM
                if grains[a] and grains[b] and grains[a] != grains[b]:
                    families.append({**pair, "grain_a": list(grains[a]), "grain_b": list(grains[b])})
                    continue          # 同指标家族的不同粒度物化,单独分档
                same_base = base(a[1], cfg.base_suffixes) == base(b[1], cfg.base_suffixes)
                (dup_pairs if same_base else cand_pairs).append(pair)
    # SQL 质量立项:join 上的行数型聚合(血缘阶段已确定性检出),口径含义寄生在
    # join 键唯一性与数据覆盖性上——底层数据变化口径即静默漂移,须人工明确计数对象
    sql_quality = []
    for uid, m in graph["models"].items():
        if layers and m["layer"] not in layers:
            continue
        for s in m.get("semantics", []):
            if s.get("type") == "join_count":
                sql_quality.append({
                    "model": disp.get(uid) or display_name(graph, uid),
                    "column": s.get("column"), "line": s.get("line"),
                    "tables": s.get("tables") or [], "join_keys": s.get("join_keys") or [],
                    "kind": "join_count",
                    "reason": "行数型聚合(count(*)/sum(1))跨 join:计数对象依赖 join 基数与数据覆盖性,SQL 未自证",
                    "suggestion": "改为 count(distinct <主键>) 或 count(<明确列>),使计数对象自证并对 join 结构免疫",
                })
    return {"duplicates": dup_pairs, "candidates": cand_pairs,
            "families": families, "agg_distinct": agg_distinct,
            "pairs_truncated": truncated, "sql_quality": sql_quality}
