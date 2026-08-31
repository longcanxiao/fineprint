#!/usr/bin/env python3
"""口径快照与漂移检测。

快照只含确定性事实(源字段集/条件指纹/语义点/表达式链/目标与取数过滤)——漂移检测建立在可复算证据上。
严重度:源字段/过滤条件/语义点(窗口去重、CASE WHEN、COALESCE、统计日)增删、目标改指向 = high;
表达式文本变化 = medium(可能是无害重构);指标集合增减 = info。
默认只记录不拦截;strict 模式为门禁语义:high 事件非零退出且基线与事件日志均不推进,
保证门禁失败可复现(第二次执行仍失败),处理后由一次通过的运行提交新基线。
"""
import contextlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from fineprint.config import MLConfig
from fineprint.i18n import t
from fineprint.project import DbtProject
from fineprint.synth import merged_trace


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace('"', "").lower()).strip()


def metric_snapshot(graph: dict, m) -> dict:
    from fineprint.tracing import resolve_model
    t = merged_trace(graph, m)
    # 目标的逻辑身份(uid.column):配置写法变化(补包名消歧/短名↔限定名)不是口径变化,
    # 漂移比较优先用它;raw target 仅作展示与老基线兼容
    try:
        _mref, _col = m.target.rsplit(".", 1)
        target_uid = f"{resolve_model(graph, _mref)}.{_col}"
    except KeyError:
        target_uid = None
    return {
        "target": m.target,
        "target_uid": target_uid,
        "query_filter": m.query_filter,
        "sources": sorted(f"{s['table']}.{s['column']}" for s in t["sources"]),
        # schema 全名版:捕捉"跨 schema 改指向"(erp.orders → crm.orders 裸名不变)
        "sources_full": sorted(f"{s.get('schema', '')}.{s['table']}.{s['column']}"
                               for s in t["sources"]),
        # 物理三段版(0.7 起):再捕捉"跨 database 改指向";格式独立成键,
        # 老基线缺此键时按既有回退链比较,不因升级误报
        "sources_full3": sorted(
            f"{s.get('database', '')}.{s.get('schema', '')}.{s['table']}.{s['column']}"
            for s in t["sources"]),
        "conditions": {c["fp"]: {"sql": c["sql"], "kind": c["kind"], "model": c["model"]}
                       for c in t["conditions"] if not c.get("is_pure_key")},
        "semantics": sorted({(s.get("type", ""), s.get("model", ""), _norm(s.get("sql")))
                             for s in t["semantics"]}),
        "exprs": {f"{e['model']}.{e['column']}": _norm(e.get("expr"))
                  for e in t["expr_chain"] if e.get("expr")},
    }


def take_snapshot(graph: dict, cfg: MLConfig) -> dict:
    return {
        # 微秒精度定宽 6 位:近邻两次运行不得互相覆盖快照文件,文件名保持字典序=时间序
        "taken_at": datetime.now().isoformat(timespec="microseconds"),
        "graph_md5": graph.get("meta", {}).get("graph_md5"),
        "graph_generated_at": graph.get("meta", {}).get("generated_at"),
        "metrics": {m.key: metric_snapshot(graph, m) for m in cfg.metrics},
    }


def snap_dir(project: DbtProject) -> Path:
    return project.workspace / "snapshots"


def drift_log_path(project: DbtProject) -> Path:
    return project.workspace / "drift_log.json"


def save_snapshot(project: DbtProject, snap: dict) -> Path:
    d = snap_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{snap['taken_at'].replace(':', '').replace('-', '').replace('.', '')}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)
    return f


def latest_snapshot(project: DbtProject) -> dict | None:
    d = snap_dir(project)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def diff_metric(key: str, old: dict, new: dict) -> list:
    ev = []

    def add(kind, severity, detail):
        ev.append({"metric_key": key, "kind": kind, "severity": severity, "detail": detail})

    # 配置层口径:目标改指向 / 取数过滤变化同为口径实质变化;老快照缺键(旧版本产物)跳过。
    # 双方都有逻辑身份(target_uid)时按它比较——写法归一(短名↔package.model 限定)
    # 不算改指向;一侧是旧快照才回退比 raw target 文本
    if old.get("target_uid") and new.get("target_uid"):
        if old["target_uid"] != new["target_uid"]:
            add("target_changed", "high", {"old": old["target"], "new": new["target"],
                                           "old_uid": old["target_uid"], "new_uid": new["target_uid"]})
    elif old.get("target") and old["target"] != new["target"]:
        add("target_changed", "high", {"old": old["target"], "new": new["target"]})
    if "query_filter" in old and old.get("query_filter") != new.get("query_filter"):
        add("query_filter_changed", "high",
            {"old": old.get("query_filter"), "new": new.get("query_filter")})
    # 源身份比较取双方共有的最全键形:物理三段(跨库改指向)→ schema 全名
    # (跨 schema 改指向)→ 裸名;一侧是旧版本快照即回退,不因格式升级误报
    skey = next((k for k in ("sources_full3", "sources_full")
                 if k in old and k in new), "sources")
    for s in sorted(set(old[skey]) - set(new[skey])):
        add("source_removed", "high", {"source": s})
    for s in sorted(set(new[skey]) - set(old[skey])):
        add("source_added", "high", {"source": s})
    oc, nc = old["conditions"], new["conditions"]
    for fp in sorted(set(oc) - set(nc)):
        add("condition_removed", "high", {"fp": fp, **oc[fp]})
    for fp in sorted(set(nc) - set(oc)):
        add("condition_added", "high", {"fp": fp, **nc[fp]})
    osem = {tuple(x) for x in old["semantics"]}
    nsem = {tuple(x) for x in new["semantics"]}
    for typ, m, sql in sorted(osem - nsem):
        add("semantic_removed", "high", {"type": typ, "model": m, "sql": sql})
    for typ, m, sql in sorted(nsem - osem):
        add("semantic_added", "high", {"type": typ, "model": m, "sql": sql})
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


def load_log(project: DbtProject) -> dict:
    f = drift_log_path(project)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"events": []}


@contextlib.contextmanager
def _file_lock(f: Path):
    """读-改-写全程互斥,防并发运行丢事件;无 fcntl 的平台(Windows)降级为无锁。"""
    lockf = f.with_suffix(".lock")
    fh = open(lockf, "w")
    try:
        with contextlib.suppress(ImportError):
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()


def append_events(project: DbtProject, events: list, from_at: str, to_at: str):
    f = drift_log_path(project)
    with _file_lock(f):
        log = load_log(project)
        now = datetime.now().isoformat(timespec="seconds")
        for e in events:
            log["events"].append({"detected_at": now, "from_snapshot": from_at, "to_snapshot": to_at, **e})
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(f)


def annotate_exposures(events: list, cfg: MLConfig, graph: dict) -> None:
    """漂移告警定向:事件挂上受影响指标目标模型的 dbt exposures(消费方名单)。
    告警从"哪个指标变了"升级为"哪些看板受影响、找谁"。目标解析失败不阻断
    (target_changed 类事件本就可能指向已不存在的模型)。"""
    from fineprint.tracing import resolve_model
    exp_map = graph.get("exposures_by_model") or {}
    if not exp_map:
        return
    by_key = {m.key: m for m in cfg.metrics}
    for e in events:
        m = by_key.get(e["metric_key"])
        if m is None:
            continue
        names = []
        for tc in (m.target, *m.extra_targets):
            try:
                uid = resolve_model(graph, tc.rsplit(".", 1)[0])
            except Exception:
                continue
            names += [x["name"] for x in exp_map.get(uid, []) if x["name"] not in names]
        if names:
            e["exposures"] = names


def run_check(project: DbtProject, cfg: MLConfig, graph: dict, save: bool = True,
              block_high: bool = False) -> list | None:
    """基线不存在则建立基线并返回 None(首跑没有"对比"这回事,不能再报"无变化");
    否则对比最近快照返回事件列表。

    save=True 时追加事件并提交新快照为基线;block_high=True(strict 门禁)且存在
    high 事件时基线与事件日志均不推进——门禁失败必须可复现,而不是失败一次后自动放行。
    """
    prev = latest_snapshot(project)
    cur = take_snapshot(graph, cfg)
    if prev is None:
        if save:
            save_snapshot(project, cur)
        print(t(f"基线快照已建立({len(cur['metrics'])} 个指标);之后的运行将与它对比",
                f"baseline snapshot established ({len(cur['metrics'])} metrics); "
                f"subsequent runs will compare against it"))
        return None
    events = diff_snapshots(prev, cur)
    annotate_exposures(events, cfg, graph)
    blocked = block_high and any(e["severity"] == "high" for e in events)
    if save and not blocked:
        if events:
            append_events(project, events, prev["taken_at"], cur["taken_at"])
        save_snapshot(project, cur)
    if blocked:
        print(t("⛔ strict 门禁: high 级漂移,基线与事件日志均不推进;"
                "确认为预期变更后去掉 --strict 运行一次以提交新基线",
                "⛔ strict gate: high-severity drift — neither the baseline nor the event log "
                "advances; once confirmed as intended, run once without --strict to commit "
                "the new baseline"), file=sys.stderr)
    return events


def print_events(events: list):
    if not events:
        print(t("口径漂移检测: 无变化", "caliber drift check: no changes"))
        return
    print(t(f"口径漂移检测: {len(events)} 个事件\n",
            f"caliber drift check: {len(events)} events\n"))
    for e in events:
        mark = {"high": "⚠", "medium": "·", "info": "i"}[e["severity"]]
        d = e["detail"]
        brief = d.get("sql") or d.get("source") or d.get("column") or ""
        tail = (t(f"  ⇒ 消费方: {', '.join(e['exposures'][:3])}",
                  f"  ⇒ consumers: {', '.join(e['exposures'][:3])}")
                if e.get("exposures") else "")
        print(f"  {mark} [{e['severity']:<6}] {e['metric_key']:<26} {e['kind']:<18} {brief[:70]}{tail}")
