#!/usr/bin/env python3
"""MetricLens 大盘取数服务:读 DuckDB 数仓(APP 层为主,人数去重类指标辅以 DWM/DWD 明细)。"""
import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from benchmark.paths import GRAPH, WORKSPACE
from metriclens.store import CaliberStore
from metriclens.trace import load_graph, trace as lineage_trace

DB = Path(__file__).resolve().parent.parent / "warehouse" / "metriclens.duckdb"
_store = CaliberStore(WORKSPACE / "store")
_graph_cache = {"graph": None}

app = FastAPI(title="MetricLens API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def q(sql: str, params=None):
    con = duckdb.connect(str(DB), read_only=True)
    try:
        cur = con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def _check_range(start: str, end: str, max_days: int = 120):
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "日期格式须为 YYYY-MM-DD")
    if s > e:
        raise HTTPException(422, f"start({start}) 不能晚于 end({end})")
    if (e - s).days + 1 > max_days:
        raise HTTPException(422, f"日期跨度不能超过 {max_days} 天")
    return s, e


def _span(start: str, end: str):
    s, e = _check_range(start, end)
    n = (e - s).days + 1
    return s, e, s - timedelta(days=n), s - timedelta(days=1)


@app.get("/api/meta")
def meta():
    r = q("select cast(min(dt) as varchar) mn, cast(max(dt) as varchar) mx from app.app_business_overview_1d")[0]
    return r


def _period_stats(s, e):
    """一段时间内 14 个指标的周期值(比率重算,人数走明细去重)。"""
    daily = q("""select * from app.app_business_overview_1d
                 where dt between ? and ? order by dt""", [s, e])
    trade = q("""select count(distinct user_id) pu,
                        count(distinct case when purchase_seq >= 2 then user_id end) ru
                 from dwm.dwm_trade_order_flag_1d where dt between ? and ?""", [s, e])[0]
    ship = q("""select count(distinct waybill_id) pw,
                       count(distinct case when sign_time is not null then waybill_id end) sw,
                       avg(case when is_presale = 0
                                then (epoch(pickup_time) - epoch(pay_time)) / 3600.0 end) ah
                from dwd.dwd_logistics_ship_detail
                where cast(pickup_time as date) between ? and ?""", [s, e])[0]
    live = q("""select coalesce(sum(gmv), 0) v from app.app_channel_overview_1d
                where attributed_channel = 'live' and dt between ? and ?""", [s, e])[0]

    def ssum(col):
        return sum((r[col] or 0) for r in daily)

    gmv, pay_amt = ssum("gmv"), ssum("pay_amt")
    po, fc = ssum("pay_order_cnt"), ssum("flash_refund_order_cnt")
    r14 = ssum("refund_amt_14d")
    pu, ru = trade["pu"] or 0, trade["ru"] or 0
    vals = {
        "gmv": gmv,
        "pay_amt": pay_amt,
        "pay_order_cnt": po,
        "pay_user_cnt": pu,
        "atv": pay_amt / pu if pu else None,
        "refund_rate_14d": r14 / pay_amt if pay_amt else None,
        "refund_amt_14d": r14,
        "flash_refund_order_ratio": fc / po if po else None,
        "delivered_rate": (ship["sw"] / ship["pw"]) if ship["pw"] else None,
        "avg_ship_hours": ship["ah"],
        "new_user_cnt": ssum("new_user_cnt"),
        "new_user_gmv_ratio": ssum("new_user_gmv") / gmv if gmv else None,
        "repurchase_rate": ru / pu if pu else None,
        "live_gmv": live["v"],
    }
    return vals, daily


SPARK_COL = {
    "gmv": "gmv", "pay_amt": "pay_amt", "pay_order_cnt": "pay_order_cnt",
    "pay_user_cnt": "pay_user_cnt", "atv": "atv", "refund_rate_14d": "refund_rate_14d",
    "refund_amt_14d": "refund_amt_14d", "flash_refund_order_ratio": "flash_refund_order_ratio",
    "delivered_rate": "delivered_rate", "avg_ship_hours": "avg_ship_hours",
    "new_user_cnt": "new_user_cnt", "new_user_gmv_ratio": "new_user_gmv_ratio",
    "repurchase_rate": "repurchase_rate",
}


@app.get("/api/overview")
def overview(start: str = Query(...), end: str = Query(...)):
    s, e, ps, pe = _span(start, end)
    cur, daily = _period_stats(s, e)
    prev, _ = _period_stats(ps, pe)
    live_spark = q("""select cast(dt as varchar) dt, sum(gmv) v from app.app_channel_overview_1d
                      where attributed_channel = 'live' and dt between ? and ? group by 1 order by 1""", [s, e])
    cards = []
    for k, v in cur.items():
        if k == "live_gmv":
            spark = [{"dt": r["dt"], "v": r["v"]} for r in live_spark]
        else:
            spark = [{"dt": str(r["dt"]), "v": r[SPARK_COL[k]]} for r in daily]
        cards.append({"key": k, "value": v, "prev": prev.get(k), "spark": spark})
    return {"start": start, "end": end, "prev_start": str(ps), "prev_end": str(pe), "cards": cards}


@app.get("/api/trend")
def trend(start: str = Query(...), end: str = Query(...)):
    _check_range(start, end)
    rows = q("""select cast(dt as varchar) dt, gmv, refund_rate_14d
                from app.app_business_overview_1d where dt between ? and ? order by dt""", [start, end])
    ch = q("""select cast(dt as varchar) dt, attributed_channel ch, sum(gmv) gmv
              from app.app_channel_overview_1d where dt between ? and ?
              group by 1, 2 order by 1""", [start, end])
    stacked = {}
    for r in ch:
        stacked.setdefault(r["dt"], {"dt": r["dt"], "app": 0, "h5": 0, "live": 0})[r["ch"]] = r["gmv"]
    return {"daily": rows, "channel": sorted(stacked.values(), key=lambda x: x["dt"])}


DIMS = {
    "channel": "attributed_channel",
    "category": "category_name",
    "province": "province",
    "live_room": "cast(live_room_id as varchar)",
}


@app.get("/api/breakdown")
def breakdown(start: str = Query(...), end: str = Query(...), dim: str = Query("channel")):
    _check_range(start, end)
    if dim not in DIMS:
        raise HTTPException(422, f"dim 须为 {sorted(DIMS)} 之一")
    col = DIMS[dim]
    extra = "and live_room_id is not null" if dim == "live_room" else ""
    rows = q(f"""select {col} as name,
                        sum(gmv) gmv, sum(pay_amt) pay_amt, sum(pay_order_cnt) pay_order_cnt,
                        sum(flash_refund_order_cnt) * 1.0 / nullif(sum(pay_order_cnt), 0) flash_ratio
                 from app.app_channel_overview_1d
                 where dt between ? and ? {extra}
                 group by 1 order by gmv desc limit 30""", [start, end])
    # 份额分母 = 该维度过滤下的全局 GMV(而非 Top 30 合计)
    grand = q(f"""select coalesce(sum(gmv), 0) v from app.app_channel_overview_1d
                  where dt between ? and ? {extra}""", [start, end])[0]["v"]
    for r in rows:
        r["share"] = (r["gmv"] or 0) / grand if grand else 0
    return {"rows": rows, "grand_total_gmv": grand, "topn_gmv": sum(r["gmv"] or 0 for r in rows)}


@app.get("/api/caliber/index")
def caliber_index():
    """口径知识库索引:只读 active 批次(整批原子发布,不存在半新半旧)。"""
    idx = _store.index()
    return idx if idx else {"run_id": None, "cards": {}, "note": "尚无已发布的口径批次"}


@app.get("/api/caliber/{key}")
def caliber_card(key: str):
    """单指标口径卡:业务口径 + 技术口径 + 双通道互验 + 溯源引用。

    只从 active 批次读取;低置信(review)卡不对外暴露技术/业务内容,
    仅返回状态占位——审核队列里的东西不上看板。
    """
    card = _store.card(key)
    if card is None:
        raise HTTPException(404, f"口径卡未生成: {key}(active 批次 {_store.active_run_id() or '无'})")
    if card.get("status") == "review":
        return {"metric_key": card["metric_key"], "title": card["title"],
                "confidence": card["confidence"], "status": "review",
                "run_id": card.get("run_id"), "generated_at": card.get("generated_at"),
                "message": "双通道互验低置信,已进入人工审核队列,口径内容暂不展示"}
    return card


@app.get("/api/lineage/{model}/{column}")
def lineage_api(model: str, column: str):
    """字段级血缘回溯:S(源字段)/F(过滤条件)/E(表达式链) 三元组,M4 口径卡的数据底座。"""
    if _graph_cache["graph"] is None:
        _graph_cache["graph"] = load_graph(GRAPH)
    try:
        return lineage_trace(_graph_cache["graph"], model, column)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/lineage/graph/{model}/{column}")
def lineage_graph_api(model: str, column: str):
    """指标链路子图(模型级节点 + 依赖边),供看板血缘画布渲染。"""
    if _graph_cache["graph"] is None:
        _graph_cache["graph"] = load_graph(GRAPH)
    g = _graph_cache["graph"]
    try:
        t = lineage_trace(g, model, column)
    except KeyError as e:
        raise HTTPException(404, str(e))
    visited = set(t["models_visited"])
    ods_tables = sorted({s["table"] for s in t["sources"]})
    nodes = [{"id": m, "layer": g["models"][m]["layer"]} for m in t["models_visited"]]
    nodes += [{"id": tb, "layer": "ods"} for tb in ods_tables]
    edges = set()
    for m in visited:
        for cinfo in g["models"][m]["columns"].values():
            for up in cinfo.get("upstreams", []):
                tb = up["table"].split(".")[-1].strip('"')
                if tb in visited or tb in ods_tables:
                    edges.add((tb, m))
    return {"target": f"{model}.{column}", "nodes": nodes,
            "edges": [{"source": a, "target": b} for a, b in sorted(edges)],
            "sources": [f"{s['table']}.{s['column']}" for s in t["sources"]]}


GOV_STORE = WORKSPACE


@app.get("/api/governance/report")
def governance_report():
    """治理报告:A 档指纹直判 + B 档 LLM 语义仲裁的重复建设/同源不同义清单。"""
    f = GOV_STORE / "governance_report.json"
    if not f.exists():
        return {"generated_at": None, "duplicates": [], "distinct": [],
                "note": "治理报告尚未生成(运行 python -m governance.arbitrate)"}
    return json.loads(f.read_text())


@app.get("/api/governance/drift")
def governance_drift(metric_key: str | None = Query(None), limit: int = Query(50, ge=1, le=500)):
    """口径漂移事件流(最新在前);metric_key 过滤单指标。"""
    f = GOV_STORE / "drift_log.json"
    events = json.loads(f.read_text())["events"] if f.exists() else []
    if metric_key:
        events = [e for e in events if e["metric_key"] == metric_key]
    return {"events": list(reversed(events))[:limit], "total": len(events)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8612)
