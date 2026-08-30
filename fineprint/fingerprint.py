# -*- coding: utf-8 -*-
"""指纹与签名原语:被口径合成(聚合锚点校验/链内配对)与治理扫描共用。

治理(governance/arbitrate)在公开发行版中可整体缺席,本模块是二者共用原语的
中立落点——核心链路不得 import 治理模块。
"""
import sqlglot
from sqlglot import exp

from fineprint.lineage import agg_one as _agg_one
from fineprint.lineage import dialect


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


def base(col: str, suffixes: list) -> str:
    """列基名:剥离物化后缀,用于 A 档同名判定与卡片治理提示。"""
    for suf in suffixes:
        col = col.removesuffix(suf)
    return col
