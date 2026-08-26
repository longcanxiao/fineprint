#!/usr/bin/env python3
"""M5 口径漂移检测:新旧快照对比 → 漂移事件流(append-only 日志)。

严重度:源字段/过滤条件的增删 = high(口径实质变化);
表达式/语义点变化 = medium;指标集合增减 = info。
默认只记录不拦截(漂移是变更告示,不是质量门禁);--strict 时 high 事件非零退出。
"""
import argparse
import json
import sys
from datetime import datetime

from governance.snapshot import STORE, latest_snapshot, save_snapshot, take_snapshot

DRIFT_LOG = STORE / "drift_log.json"


def diff_metric(key: str, old: dict, new: dict) -> list:
    ev = []

    def add(kind, severity, detail):
        ev.append({"metric_key": key, "kind": kind, "severity": severity, "detail": detail})

    for s in sorted(set(old["sources"]) - set(new["sources"])):
        add("source_removed", "high", {"source": s})
    for s in sorted(set(new["sources"]) - set(old["sources"])):
        add("source_added", "high", {"source": s})
    oc, nc = old["conditions"], new["conditions"]
    for fp in sorted(set(oc) - set(nc)):
        add("condition_removed", "high", {"fp": fp, **oc[fp]})
    for fp in sorted(set(nc) - set(oc)):
        add("condition_added", "high", {"fp": fp, **nc[fp]})
    # 语义点(窗口去重/CASE WHEN/COALESCE/统计日)的增删 = 口径实质变化,与条件增删同级
    osem = {tuple(x) for x in old["semantics"]}
    nsem = {tuple(x) for x in new["semantics"]}
    for t, m, sql in sorted(osem - nsem):
        add("semantic_removed", "high", {"type": t, "model": m, "sql": sql})
    for t, m, sql in sorted(nsem - osem):
        add("semantic_added", "high", {"type": t, "model": m, "sql": sql})
    oe, ne = old["exprs"], new["exprs"]
    for col in sorted(set(oe) & set(ne)):
        if oe[col] != ne[col]:
            add("expr_changed", "medium", {"column": col, "old": oe[col], "new": ne[col]})
    for col in sorted(set(oe) - set(ne)):
        add("expr_removed", "medium", {"column": col, "old": oe[col]})
    for col in sorted(set(ne) - set(oe)):
        add("expr_added", "medium", {"column": col, "new": ne[col]})
    return ev


def diff_snapshots(old: dict, new: dict) -> list:
    events = []
    om, nm = old["metrics"], new["metrics"]
    for k in sorted(set(om) - set(nm)):
        events.append({"metric_key": k, "kind": "metric_removed", "severity": "info", "detail": {}})
    for k in sorted(set(nm) - set(om)):
        events.append({"metric_key": k, "kind": "metric_added", "severity": "info", "detail": {}})
    for k in sorted(set(om) & set(nm)):
        events += diff_metric(k, om[k], nm[k])
    return events


def load_log() -> dict:
    if DRIFT_LOG.exists():
        return json.loads(DRIFT_LOG.read_text())
    return {"events": []}


def append_events(events: list, from_at: str, to_at: str):
    log = load_log()
    now = datetime.now().isoformat(timespec="seconds")
    for e in events:
        log["events"].append({"detected_at": now, "from_snapshot": from_at, "to_snapshot": to_at, **e})
    tmp = DRIFT_LOG.with_suffix(".tmp")
    tmp.write_text(json.dumps(log, ensure_ascii=False, indent=1))
    tmp.replace(DRIFT_LOG)


def run_check(save: bool = True) -> list:
    """基线不存在则建立基线(无事件);否则对比最近快照并追加事件、保存新快照。"""
    prev = latest_snapshot()
    cur = take_snapshot()
    if prev is None:
        if save:
            save_snapshot(cur)
        print(f"基线快照已建立({len(cur['metrics'])} 个指标),无对比对象")
        return []
    events = diff_snapshots(prev, cur)
    if save:
        if events:
            append_events(events, prev["taken_at"], cur["taken_at"])
        save_snapshot(cur)
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="high 级漂移事件时非零退出")
    ap.add_argument("--dry-run", action="store_true", help="只对比不落盘")
    args = ap.parse_args()
    events = run_check(save=not args.dry_run)
    if not events:
        print("口径漂移检测: 无变化")
        return
    print(f"口径漂移检测: {len(events)} 个事件\n")
    for e in events:
        mark = {"high": "⚠", "medium": "·", "info": "i"}[e["severity"]]
        d = e["detail"]
        brief = d.get("sql") or d.get("source") or d.get("column") or ""
        print(f"  {mark} [{e['severity']:<6}] {e['metric_key']:<26} {e['kind']:<18} {brief[:70]}")
    if args.strict and any(e["severity"] == "high" for e in events):
        sys.exit(1)


if __name__ == "__main__":
    main()
