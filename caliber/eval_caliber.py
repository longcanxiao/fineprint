#!/usr/bin/env python3
"""M4 验收:14 道口径陷阱是否被口径卡揭示(≥12 通过)。

判定原则:陷阱的关键事实必须出现在对应指标口径卡的"业务口径/技术口径/特殊处理"文本中
(不含 trace 原始条件——那是 M3 的产出;这里考察的是合成表述是否把陷阱讲了出来)。
"""
import json
import re
import sys

from caliber.store_paths import active_dir


def _card_file(key: str):
    d = active_dir()
    return (d / f"{key}.json") if d else None


def load_card(key: str) -> dict:
    f = _card_file(key)
    if not f or not f.exists():
        return {}
    return json.loads(f.read_text())


def card_text(key: str) -> str:
    d = load_card(key)
    if not d:
        return ""
    parts = [json.dumps(d.get("technical", {}), ensure_ascii=False),
             json.dumps(d.get("business", {}), ensure_ascii=False)]
    return re.sub(r"\s+", "", " ".join(parts))


def has(key: str, *patterns: str) -> bool:
    t = card_text(key)
    return all(any(re.search(p, t) for p in pat.split("|")) for pat in patterns)


def sources_of(key: str):
    d = load_card(key)
    if not d:
        return set()
    return {f"{s['table']}.{s['column']}" for s in d["trace"]["sources"]}


CHECKS = [
    ("T1", "GMV 剔秒退", lambda: has("gmv", "秒退|60秒|60 秒")),
    ("T2", "退款 14 天窗口", lambda: has("refund_rate_14d", "14", "支付")),
    ("T3", "新客=首购口径", lambda: has("new_user_cnt", "首笔|首次|第一笔")),
    ("T4", "妥投分母=揽收", lambda: has("delivered_rate", "揽收")),
    ("T5", "客单价按人", lambda: has("atv", "人数|按人|去重.{0,4}用户|用户数")),
    ("T6", "发货时长剔预售+揽收锚点", lambda: has("avg_ship_hours", "预售", "揽收")),
    ("T7", "退款率同名不同义", lambda: has("refund_rate_14d", "14")
        and has("dm_refund_rate", "当日|不限|自然日")
        and not has("dm_refund_rate", "14天|14 天|≤14|<=14")),
    ("T8", "退款金额重复建设", lambda: True),   # 实际判定见 main() 中的 t8:卡片级治理揭示
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
    # T8:重复建设必须在"卡片上"被揭示——refund_amt_14d 卡的 governance.duplicates
    # 须包含指纹扫描自动发现的靶向对(不只验证扫描器本身)
    def t8():
        target = {"dm_trade_stats_1d.refund_amt", "dm_after_sale_stats_1d.refund_amt_total"}
        dups = (load_card("refund_amt_14d").get("governance") or {}).get("duplicates") or []
        return any({p.get("a"), p.get("b")} == target for p in dups)
    checks = [(tid, name, t8 if tid == "T8" else fn) for tid, name, fn in CHECKS]
    oks = 0
    print("=== 口径卡陷阱揭示评测(M4)===\n")
    for tid, name, fn in checks:
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
