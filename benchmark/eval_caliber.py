#!/usr/bin/env python3
"""口径卡验收:14 道口径陷阱是否被口径卡揭示(≥12 通过)。

判定原则:陷阱的关键事实必须出现在对应指标口径卡的"业务口径/技术口径"文本中;
T8 断言卡片级治理揭示(refund_amt_14d 卡的 governance.duplicates 含靶向对)。
"""
import json
import re
import sys

from benchmark.paths import WORKSPACE
from fineprint.store import CaliberStore

STORE = CaliberStore(WORKSPACE / "store")
T8_PAIR = {"dm_trade_stats_1d.refund_amt", "dm_after_sale_stats_1d.refund_amt_total"}


def card(key: str) -> dict:
    return STORE.card(key) or {}


def card_text(key: str) -> str:
    d = card(key)
    parts = [json.dumps(d.get("technical", {}), ensure_ascii=False),
             json.dumps(d.get("business", {}), ensure_ascii=False)]
    return re.sub(r"\s+", "", " ".join(parts))


def has(key: str, *patterns: str) -> bool:
    t = card_text(key)
    return all(any(re.search(p, t) for p in pat.split("|")) for pat in patterns)


def t8_revealed() -> bool:
    dups = (card("refund_amt_14d").get("governance") or {}).get("duplicates") or []
    return any({p["a"], p["b"]} == T8_PAIR for p in dups)


CHECKS = [
    ("T1", "GMV 剔秒退", lambda: has("gmv", "秒退|60秒|60 秒")),
    ("T2", "退款 14 天窗口", lambda: has("refund_rate_14d", "14", "支付")),
    ("T3", "新客=首购口径", lambda: has("new_user_cnt", "首笔|首次|第一笔")),
    ("T4", "妥投分母=揽收", lambda: has("delivered_rate", "揽收")),
    ("T5", "客单价按人", lambda: has("atv", "人数|按人|去重.{0,4}用户|用户数")),
    ("T6", "发货时长剔预售+揽收锚点", lambda: has("avg_ship_hours", "预售", "揽收")),
    # 负向断言只拦"自称 14 天窗口"的表述;"与 APP 层 14 天口径同名不同义"这类对比说明是正向揭示
    ("T7", "退款率同名不同义", lambda: has("refund_rate_14d", "14")
        and has("dm_refund_rate", "当日|不限|自然日")
        and not has("dm_refund_rate", "≤14|<=14|14天内|14 天内|限14")),
    ("T8", "退款金额重复建设(卡片级揭示)", t8_revealed),
    ("T9", "多版本去重", lambda: has("gmv", "去重|最新版本|多版本")),
    ("T10", "复购=第2笔序号", lambda: has("repurchase_rate", "第2|第 2|第二|≥2|>=2|2笔|2 笔|序号")),
    ("T11", "直播延迟归因", lambda: has("live_gmv", "30分钟|30 分钟")),
    ("T12", "汇率折算", lambda: has("gmv", "汇率")),
    ("T13", "打款缺失兜底", lambda: has("refund_amt_14d", "申请金额|兜底|缺失")
        or has("refund_rate_14d", "申请金额|兜底|缺失")),
    ("T14", "统计日=退款完成日", lambda: has("refund_amt_14d", "退款完成|到账|统计日")
        or has("refund_rate_14d", "退款完成|到账|统计日")),
]


def main():
    # 前置硬门禁:卡片必须由当前血缘图生成——图重建后旧卡的揭示命中不作数
    from benchmark.paths import GRAPH
    from fineprint.tracing import load_graph
    cur_md5 = load_graph(GRAPH)["meta"].get("graph_md5")
    keys = [k for k in (STORE.index() or {}).get("cards", {})]
    stale = [k for k in keys if card(k).get("graph_md5") != cur_md5]
    if not keys or stale:
        print("=== 口径卡陷阱揭示评测 ===\n")
        print(f"  ✗ 口径卡与当前血缘图版本不一致(过期卡: {stale or '无卡'});"
              f"请先 fineprint synth 重新生成整批后再验收: FAIL ❌")
        sys.exit(1)

    oks = 0
    print("=== 口径卡陷阱揭示评测 ===\n")
    for tid, name, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        oks += ok
        print(f"  {'✓' if ok else '✗'} {tid:<4} {name}")
    print(f"\n  揭示 {oks}/14,验收线 ≥12:", "PASS ✅" if oks >= 12 else "FAIL ❌")
    sys.exit(0 if oks >= 12 else 1)


if __name__ == "__main__":
    main()
