#!/usr/bin/env python3
"""M5 验收:治理报告与漂移引擎的端到端断言。

1. 治理报告存在,T8 靶向对(交易域/售后域退款金额)判为重复;
2. B 档候选全部有 LLM 仲裁结论(verdict + 判据),且"计数 vs 比率"类正确判 distinct;
3. 漂移日志含 14 天窗口演练事件(high 级,变更 + 回归成对);
4. 当前图与最新快照对比无漂移(账实一致)。
"""
import json
import sys

from governance.drift import DRIFT_LOG, diff_snapshots
from governance.snapshot import latest_snapshot, take_snapshot
from governance.arbitrate import REPORT

T8_PAIR = {"dm_trade_stats_1d.refund_amt", "dm_after_sale_stats_1d.refund_amt_total"}


def main():
    oks, checks = 0, []

    report = json.loads(REPORT.read_text()) if REPORT.exists() else None
    checks.append(("治理报告已生成", report is not None))
    if report:
        checks.append(("T8 靶向对判为重复建设",
                       any({p["a"], p["b"]} == T8_PAIR for p in report["duplicates"])))
        b_items = [p for p in report["duplicates"] + report["distinct"] if p["tier"] == "B"]
        checks.append(("B 档候选全部有仲裁结论",
                       len(b_items) == report.get("b_tier_pairs", -1)
                       and all(p.get("verdict") in ("duplicate", "distinct") and p.get("reason")
                               for p in b_items)))
        rate_pair = {"app_business_overview_1d.delivered_rate", "dm_logistics_stats_1d.sign_waybill_cnt"}
        checks.append(("计数 vs 比率对判为同源不同义",
                       any({p["a"], p["b"]} == rate_pair for p in report["distinct"])))

    log = json.loads(DRIFT_LOG.read_text())["events"] if DRIFT_LOG.exists() else []
    drill = [e for e in log if e["metric_key"] == "refund_rate_14d" and e["severity"] == "high"]
    checks.append(("漂移日志含 14 天窗口演练事件(high)",
                   any("15" in (e["detail"].get("sql") or "") and e["kind"] == "semantic_added" for e in drill)
                   and any("14" in (e["detail"].get("sql") or "") and e["kind"] == "semantic_added" for e in drill)))

    prev = latest_snapshot()
    live = diff_snapshots(prev, take_snapshot()) if prev else None
    checks.append(("最新快照与当前图一致(无未记录漂移)", live == []))

    print("=== 治理与漂移评测(M5)===\n")
    for name, ok in checks:
        oks += bool(ok)
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"\n  通过 {oks}/{len(checks)}:", "PASS ✅" if oks == len(checks) else "FAIL ❌")
    sys.exit(0 if oks == len(checks) else 1)


if __name__ == "__main__":
    main()
