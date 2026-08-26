#!/usr/bin/env python3
"""M5 口径快照:对看板全部指标的血缘回溯结果做规范化留痕,供漂移对比。

快照只含确定性事实(源字段集/条件指纹/语义点/表达式链),不含 LLM 产物——
漂移检测必须建立在可复算的证据上。
"""
import json
import re
from datetime import datetime
from pathlib import Path

from caliber.pipeline import METRICS
from lineage.trace import load_graph, trace

STORE = Path(__file__).resolve().parent / "store"
SNAP_DIR = STORE / "snapshots"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace('"', "").lower()).strip()


def metric_snapshot(graph: dict, m: dict) -> dict:
    """单指标规范化快照(含 extra_targets 的合并回溯,与口径卡同口径)。"""
    model, col = m["target"].rsplit(".", 1)
    t = trace(graph, model, col)
    for et in m.get("extra_targets", []):
        em, ec = et.rsplit(".", 1)
        t2 = trace(graph, em, ec)
        seen_fp = {c["fp"] for c in t["conditions"]}
        t["conditions"] += [c for c in t2["conditions"] if c["fp"] not in seen_fp]
        t["semantics"] += [s for s in t2["semantics"] if s not in t["semantics"]]
        t["expr_chain"] += [e for e in t2["expr_chain"] if e not in t["expr_chain"]]
        t["sources"] += [s for s in t2["sources"] if s not in t["sources"]]
    return {
        "target": m["target"],
        "sources": sorted(f"{s['table']}.{s['column']}" for s in t["sources"]),
        "conditions": {c["fp"]: {"sql": c["sql"], "kind": c["kind"], "model": c["model"]}
                       for c in t["conditions"] if not c.get("is_pure_key")},
        "semantics": sorted({(s.get("type", ""), s.get("model", ""), _norm(s.get("sql")))
                             for s in t["semantics"]}),
        "exprs": {f"{e['model']}.{e['column']}": _norm(e.get("expr"))
                  for e in t["expr_chain"] if e.get("expr")},
    }


def take_snapshot(graph: dict | None = None) -> dict:
    graph = graph or load_graph()
    return {
        "taken_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": {m["key"]: metric_snapshot(graph, m) for m in METRICS},
    }


def save_snapshot(snap: dict) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = snap["taken_at"].replace(":", "").replace("-", "")
    f = SNAP_DIR / f"{stamp}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
    tmp.replace(f)
    return f


def latest_snapshot() -> dict | None:
    if not SNAP_DIR.is_dir():
        return None
    files = sorted(SNAP_DIR.glob("*.json"))
    return json.loads(files[-1].read_text()) if files else None


if __name__ == "__main__":
    f = save_snapshot(take_snapshot())
    print(f"快照已保存: {f}")
