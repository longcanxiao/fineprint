# -*- coding: utf-8 -*-
"""口径树:公式按最外层运算劈分,条件按行集闭包归入分支/公共组;直通列下钻定义层。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fineprint.lineage import build_graph  # noqa: E402
from fineprint.tracing import render, trace  # noqa: E402
from fineprint.tree import caliber_tree, render_tree  # noqa: E402
from tests.test_generalize import _cat, _node, make_project  # noqa: E402

RATE_SQL = """
with paid as (
    select order_id, amount, paid_at from main.stg_o where status = 'paid'
),
r14 as (
    select r.order_id, sum(r.refund_amount) as refund_amount
    from main.stg_r r join paid o on r.order_id = o.order_id
    where r.refunded_at <= o.paid_at + interval 14 day
    group by r.order_id
)
select
    cast(o.paid_at as date) as stat_date,
    round(sum(coalesce(r.refund_amount, 0)) / sum(o.amount), 6) as rate
from paid o left join r14 r on o.order_id = r.order_id
group by 1
"""


def _proj(tmp_path):
    cats = {
        "seed.p.raw_o": _cat("main", "raw_o",
                             {"order_id": "INT", "amount": "DOUBLE", "status": "TEXT",
                              "paid_at": "TIMESTAMP", "is_test": "INT"}),
        "seed.p.raw_r": _cat("main", "raw_r",
                             {"refund_id": "TEXT", "order_id": "INT",
                              "refund_amount": "DOUBLE", "refunded_at": "TIMESTAMP"}),
        "model.p.stg_o": _cat("main", "stg_o",
                              {"order_id": "INT", "amount": "DOUBLE", "status": "TEXT",
                               "paid_at": "TIMESTAMP"}),
        "model.p.stg_r": _cat("main", "stg_r",
                              {"refund_id": "TEXT", "order_id": "INT",
                               "refund_amount": "DOUBLE", "refunded_at": "TIMESTAMP"}),
        "model.p.dm_rate": _cat("main", "dm_rate",
                                {"stat_date": "DATE", "rate": "DOUBLE"}),
        "model.p.app_rate": _cat("main", "app_rate",
                                 {"stat_date": "DATE", "rate": "DOUBLE"}),
    }
    nodes = {
        "model.p.stg_o": {**_node("stg_o", "compiled/stg_o.sql"),
                          "depends_on": {"nodes": []}},
        "model.p.stg_r": {**_node("stg_r", "compiled/stg_r.sql"),
                          "depends_on": {"nodes": []}},
        "model.p.dm_rate": {**_node("dm_rate", "compiled/dm_rate.sql"),
                            "depends_on": {"nodes": ["model.p.stg_o", "model.p.stg_r"]}},
        "model.p.app_rate": {**_node("app_rate", "compiled/app_rate.sql"),
                             "depends_on": {"nodes": ["model.p.dm_rate"]}},
    }
    return make_project(
        tmp_path, nodes=nodes, catalog_nodes=cats,
        sqls={
            "compiled/stg_o.sql":
                "select order_id, amount, status, paid_at from main.raw_o where is_test = 0",
            "compiled/stg_r.sql":
                "select refund_id, order_id, refund_amount, refunded_at from ("
                "  select *, row_number() over (partition by refund_id order by refunded_at desc) as rn"
                "  from main.raw_r) t where rn = 1",
            "compiled/dm_rate.sql": RATE_SQL,
            "compiled/app_rate.sql": "select stat_date, rate from main.dm_rate",
        })


def _tree(p, model, col):
    g = build_graph(p)
    uid = f"model.p.{model}"
    t = trace(g, uid, col)
    return caliber_tree(p, g, uid, col, t), t


class TestCaliberTree:
    def test_division_split_and_attribution(self, tmp_path):
        tr, _ = _tree(_proj(tmp_path), "dm_rate", "rate")
        assert tr["op"] == "÷" and "ROUND(A / B, 6)" == tr["skeleton"]
        a, b = tr["branches"]
        assert (a["label"], b["label"]) == ("分子", "分母")
        a_sqls = " ".join(c["sql"] for c in a["conds"])
        assert "INTERVAL" in a_sqls and "rn = 1" in a_sqls     # 窗口+去重归分子
        assert not b["conds"]                                  # 分母无专属条件
        common = " ".join(c["sql"] for c in tr["common"])
        assert "status" in common and "is_test" in common      # join 约束两侧 → 公共

    def test_passthrough_descends_to_defining_model(self, tmp_path):
        tr, _ = _tree(_proj(tmp_path), "app_rate", "rate")
        assert tr is not None and tr["defined_in"] == "dm_rate"
        assert tr["op"] == "÷"                                 # 树建在定义层,照样劈分

    def test_plain_agg_single_branch(self, tmp_path):
        p = _proj(tmp_path)
        g = build_graph(p)
        t = trace(g, "model.p.stg_o", "amount")
        tr = caliber_tree(p, g, "model.p.stg_o", "amount", t)
        if tr is not None:                                     # 直通列可退化为不可树化
            assert tr["op"] is None and len(tr["branches"]) == 1

    def test_render_tree_and_fallback(self, tmp_path):
        p = _proj(tmp_path)
        g = build_graph(p)
        t = trace(g, "model.p.dm_rate", "rate")
        tr = caliber_tree(p, g, "model.p.dm_rate", "rate", t)
        txt = render_tree(tr)
        assert "分子" in txt and "两侧共同口径" in txt
        out = render(t, tree=txt)
        assert "表达式链 E" not in out and "分子" in out       # 树替代 E 块
        assert "源字段 S" not in out and "--full" in out       # 默认收敛:锚点归 --full
        full_txt = render_tree(tr, full=True)
        assert "源:" in full_txt and " L" in full_txt          # 源字段与出处锚点长在树上
        assert "models/" in full_txt                            # 条件行带源文件路径
        full = render(t, tree=full_txt, full=True)
        assert "源字段 S" not in full and "过滤条件 F" not in full   # 平铺块不再重复
        assert "表达式链 E" in render(t)                        # 无树时平铺视图不变
        assert "源字段 S" in render(t, full=True)               # 无树回退仍有完整明细
