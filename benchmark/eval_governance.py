#!/usr/bin/env python3
"""治理与漂移验收:治理报告、T8 靶向对、B 档仲裁、漂移演练事件、账实一致。"""
import json
import sys

from benchmark.paths import GRAPH, PROJECT_DIR, WORKSPACE
from metriclens.config import MLConfig
from metriclens.drift import diff_snapshots, latest_snapshot, take_snapshot
from metriclens.project import DbtProject
from metriclens.trace import load_graph

T8_PAIR = {"dm_trade_stats_1d.refund_amt", "dm_after_sale_stats_1d.refund_amt_total"}


def main():
    project = DbtProject(PROJECT_DIR)
    cfg = MLConfig.load(PROJECT_DIR)
    graph = load_graph(GRAPH)
    oks, checks = 0, []

    rf = WORKSPACE / "governance_report.json"
    report = json.loads(rf.read_text()) if rf.exists() else None
    checks.append(("治理报告已生成", report is not None))
    if report:
        # 产物必须绑定当前图:图重建后旧报告的"绿灯"不作数(混版本验收无效)
        checks.append(("治理报告与当前血缘图同版本(graph_md5 一致)",
                       report.get("graph_md5") == graph["meta"].get("graph_md5")))
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

    lf = WORKSPACE / "drift_log.json"
    log = json.loads(lf.read_text())["events"] if lf.exists() else []
    drill = [e for e in log if e["metric_key"] == "refund_rate_14d" and e["severity"] == "high"]
    checks.append(("漂移日志含 14 天窗口演练事件(high)",
                   any("15" in (e["detail"].get("sql") or "") and e["kind"] == "semantic_added" for e in drill)
                   and any("14" in (e["detail"].get("sql") or "") and e["kind"] == "semantic_added" for e in drill)))

    prev = latest_snapshot(project)
    live = diff_snapshots(prev, take_snapshot(graph, cfg)) if prev else None
    checks.append(("最新快照与当前图一致(无未记录漂移)", live == []))
    checks.append(("最新快照与当前血缘图同版本(graph_md5 一致)",
                   bool(prev) and prev.get("graph_md5") == graph["meta"].get("graph_md5")))

    print("=== 治理与漂移评测 ===\n")
    for name, ok in checks:
        oks += bool(ok)
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"\n  通过 {oks}/{len(checks)}:", "PASS ✅" if oks == len(checks) else "FAIL ❌")
    sys.exit(0 if oks == len(checks) else 1)


if __name__ == "__main__":
    main()
