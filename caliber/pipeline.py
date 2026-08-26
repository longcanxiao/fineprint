#!/usr/bin/env python3
"""M4 口径合成流水线:双通道互验 + 技术/业务口径生成 + 口径知识库落盘。

通道一 = M3 血缘引擎的确定性 S₁/F₁/E₁(lineage.trace);
通道二 = LLM 逐跳解析单模型 SQL(独立输入,不喂通道一结果),原文引用机器校验防幻觉;
互验分歧 → 置信分级,低置信进审核队列不上看板。
"""
import argparse
import json
import re
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yaml

from caliber.llm import chat_json, fast_model, quality_model
from caliber.store_paths import RUNS, STORE, activate, active_dir
from lineage.core import DIALECT, normalize_condition
from lineage.trace import load_graph, trace

import sqlglot

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "warehouse" / "dbt_project" / "target" / "manifest.json"

# 看板 14 指标 + T7 治理对比卡(dm 当日退款率)
METRICS = [
    {"key": "gmv", "title": "GMV", "target": "app_business_overview_1d.gmv"},
    {"key": "pay_amt", "title": "支付金额", "target": "app_business_overview_1d.pay_amt"},
    {"key": "pay_order_cnt", "title": "支付订单数", "target": "app_business_overview_1d.pay_order_cnt"},
    {"key": "pay_user_cnt", "title": "支付人数", "target": "app_business_overview_1d.pay_user_cnt"},
    {"key": "atv", "title": "客单价", "target": "app_business_overview_1d.atv"},
    {"key": "refund_rate_14d", "title": "近14天退款率", "target": "app_business_overview_1d.refund_rate_14d"},
    {"key": "refund_amt_14d", "title": "退款金额(14天口径)", "target": "app_business_overview_1d.refund_amt_14d"},
    {"key": "flash_refund_order_ratio", "title": "秒退单占比", "target": "app_business_overview_1d.flash_refund_order_ratio"},
    {"key": "delivered_rate", "title": "妥投率", "target": "app_business_overview_1d.delivered_rate"},
    {"key": "avg_ship_hours", "title": "平均发货时长", "target": "app_business_overview_1d.avg_ship_hours"},
    {"key": "new_user_cnt", "title": "新客数", "target": "app_business_overview_1d.new_user_cnt"},
    {"key": "new_user_gmv_ratio", "title": "新客GMV占比", "target": "app_business_overview_1d.new_user_gmv_ratio"},
    {"key": "repurchase_rate", "title": "复购率", "target": "app_business_overview_1d.repurchase_rate"},
    {"key": "live_gmv", "title": "渠道归因GMV·直播", "target": "app_channel_overview_1d.gmv",
     "extra_targets": ["app_channel_overview_1d.attributed_channel"],
     "query_filter": "attributed_channel = 'live'(取归因渠道为直播间的行)"},
    {"key": "dm_refund_rate", "title": "退款率(DM 当日口径·治理对比)", "target": "dm_trade_stats_1d.refund_rate"},
]


def load_column_docs():
    mf = json.load(open(MANIFEST))
    docs = {}
    for coll in (mf.get("nodes", {}), mf.get("sources", {})):
        for node in coll.values():
            tbl = node.get("name")
            for col, meta in (node.get("columns") or {}).items():
                if meta.get("description"):
                    docs.setdefault(tbl, {})[col] = meta["description"]
    return docs


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace('"', "").lower()).strip()


# ---------------- LLM 输出结构校验(P1-2:不只验证"是 JSON") ----------------
def _need(obj, key, typ, ctx):
    v = obj.get(key)
    if not isinstance(v, typ):
        raise ValueError(f"{ctx}.{key} 类型错误: 期望 {typ}, 实得 {type(v).__name__}")
    return v


def validate_hop(obj: dict):
    cols = _need(obj, "columns", dict, "hop")
    for c, info in cols.items():
        _need(info, "source_columns", list, f"hop.columns.{c}")
        for sc in info["source_columns"]:
            _need(sc, "table", str, f"hop.columns.{c}.source_columns[]")
            _need(sc, "column", str, f"hop.columns.{c}.source_columns[]")
    for f in _need(obj, "filters", list, "hop"):
        _need(f, "quote", str, "hop.filters[]")
        _need(f, "kind", str, "hop.filters[]")


def validate_merge(obj: dict):
    _need(obj, "formula", str, "merge")
    _need(obj, "summary", str, "merge")
    for kf in obj.get("key_filters") or []:
        _need(kf, "text", str, "merge.key_filters[]")


def validate_biz(obj: dict):
    _need(obj, "definition", str, "biz")
    for c in _need(obj, "clauses", list, "biz"):
        _need(c, "text", str, "biz.clauses[]")
        _need(c, "evidence_ids", list, "biz.clauses[]")


# ---------------- 通道二:逐跳抽取 ----------------
HOP_SYSTEM = """你是数据仓库 SQL 口径分析器。给你一个 dbt 模型的完整 SQL 与若干目标输出列,逐列提取"本模型这一跳"的口径信息。
只输出 JSON,结构:
{"columns": {"<目标列名>": {
  "expression": "该列的计算表达式(化简但保语义)",
  "source_columns": [{"table": "真实上游表名(CTE 需展开到其引用的表)", "column": "列名"}],
  "special": ["影响该列语义的特殊处理:窗口去重/序号、COALESCE 兜底、单位或币种换算、CASE WHEN 归因、时间窗、统计日归属等;无则空数组"]
}},
 "filters": [{"quote": "SQL 原文逐字符片段", "kind": "where|join_on|qualify|having", "effect": "该条件的作用一句话"}]}
硬性要求:
1. filters 覆盖影响输出行集或列取值的全部条件(含 join on 中的业务限定),quote 必须逐字符摘自给定 SQL——它会被机器校验,不在原文中的引用会被判为幻觉;
2. source_columns 只写真实物理上游表(给定 SQL 的 FROM/JOIN 里的库表,穿透 CTE),不要写 CTE 名;
3. 不要臆造给定 SQL 之外的任何信息。"""


def verify_quotes(out: dict, sql: str) -> dict:
    """原文引用机器校验(纯函数):空引用与不在原文中的引用一律判失败。"""
    nsql = norm_text(sql)
    for f in out.get("filters", []):
        nq = norm_text(f.get("quote", ""))
        f["quote_verified"] = bool(nq) and nq in nsql
    return out


def extract_hop(model_name: str, sql: str, cols: list, layer: str) -> dict:
    user = f"模型: {model_name}(分层 {layer})\n目标输出列: {', '.join(cols)}\n\nSQL:\n```sql\n{sql}\n```"
    out = chat_json(HOP_SYSTEM, user, max_tokens=3000, model=fast_model(), validator=validate_hop)
    return verify_quotes(out, sql)


# ---------------- 归并 ----------------
MERGE_SYSTEM = """你是指标口径归并器。输入某指标沿数仓链路(APP←DM←DWM←DWD←ODS)逐跳提取的结构化口径,输出端到端技术口径 JSON:
{"formula": "端到端等效计算式(业务可读伪 SQL,一行)",
 "window": "时间窗与统计日归属说明(无则空串)",
 "special": ["合并去重后的特殊处理清单"],
 "key_filters": [{"text": "关键过滤条件(合并等价项,剔除纯关联键)", "layer": "生效分层"}],
 "summary": "2-3 句话的技术口径摘要"}
只依据输入归并化简,不新增事实;同义条件合并为一条。只输出 JSON。"""


def merge_hops(title: str, target: str, hops: list) -> dict:
    user = f"指标: {title}(目标列 {target})\n逐跳口径(自 APP 向 ODS):\n{json.dumps(hops, ensure_ascii=False, indent=1)}"
    return chat_json(MERGE_SYSTEM, user, max_tokens=8000, model=quality_model(), validator=validate_merge)


# ---------------- 业务口径 ----------------
BIZ_SYSTEM = """你是指标业务口径撰写器。输入:已互验的技术口径、编号证据清单(来自血缘引擎与机器校验过的 SQL 原文)、相关字段的业务注释、业务词典。输出业务人员能直接读懂的口径 JSON:
{"definition": "一句话业务口径(≤60 字,说清分子分母/范围/窗口)",
 "clauses": [{"text": "业务化条款(如:剔除支付后60秒内退款的秒退单)",
              "basis": "所引证据的原文片段(照抄,可截断)",
              "evidence_ids": ["E1", "Q2"]}],
 "caveats": ["使用注意(如统计日归属造成的解读陷阱),没有则空数组"]}
硬性要求:
1. 每条 clause 必须给出非空 evidence_ids,且只能引用证据清单中存在的编号;basis 照抄所引证据文本;
2. 无法绑定任何证据编号的信息不得写入 clauses,只能写入 caveats 并以"(无确定性证据)"结尾;
3. 只使用输入中存在的事实与术语;字段缺业务注释时用技术直译并标注"(待补充业务注释)";禁止引入任何输入之外的业务假设。只输出 JSON。"""


def build_evidence(t: dict, hops_seq: list) -> list:
    """确定性证据清单:通道一条件(E)/语义点(S)/表达式链(X)+ 已互验匹配的原文引用(Q)。

    业务条款只能引用这些 ID——证据只来自血缘、SQL AST 与机器校验过的原文,
    不含 LLM 归并产物,避免"LLM 引用 LLM"的循环证据(P1-B)。
    """
    ev, seen = [], set()

    def add(prefix, kind, text, model=None, line=None):
        key = (kind, norm_text(str(text or "")))
        if not text or key in seen:
            return
        seen.add(key)
        n = sum(1 for e in ev if e["id"].startswith(prefix)) + 1
        ev.append({"id": f"{prefix}{n}", "kind": kind, "model": model, "line": line, "text": str(text)})

    for c in t["conditions"]:
        if not c.get("is_pure_key"):
            add("E", f"condition:{c.get('kind', '')}", c["sql"], c.get("model"), c.get("line"))
    for s in t["semantics"]:
        add("S", f"semantic:{s.get('type', '')}", s.get("sql"), s.get("model"), s.get("line"))
    for e in t["expr_chain"]:
        if e.get("expr"):
            add("X", "expression", f"{e['column']} = {e['expr']}", e.get("model"))
    for h in hops_seq:
        for f in h.get("filters", []):
            if f.get("match") == "matched":
                add("Q", f"verified_quote:{f.get('kind', '')}", f.get("quote"), h.get("model"))
    return ev


def gen_business(title: str, technical: dict, docs_ctx: dict, lexicon: dict,
                 query_filter: str | None, evidence: list) -> dict:
    ev_lines = "\n".join(f"[{e['id']}] ({e['kind']}, {e.get('model') or '-'}) {e['text']}" for e in evidence)
    user = (f"指标: {title}\n"
            + (f"取数过滤: {query_filter}\n" if query_filter else "")
            + f"技术口径:\n{json.dumps(technical, ensure_ascii=False, indent=1)}\n\n"
            f"证据清单(clauses 只能引用以下编号):\n{ev_lines}\n\n"
            f"相关字段业务注释:\n{json.dumps(docs_ctx, ensure_ascii=False, indent=1)}\n\n"
            f"业务词典:\n{json.dumps(lexicon, ensure_ascii=False, indent=1)}")
    return chat_json(BIZ_SYSTEM, user, max_tokens=8000, model=quality_model(), validator=validate_biz)


# ---------------- 互验 ----------------
def classify_filters(t: dict, hops_by_model: dict, graph: dict) -> dict:
    """给通道二每条 filter 打相关性标注(match 字段),并返回聚合计数。

    matched      与通道一 scope 内条件指纹一致 → 本指标真实口径,可进归并与证据池
    pure_key     纯关联键(a.x=b.x / using),无业务语义
    out_of_scope 指纹在途经模型全条件集但不在本指标 scope → 同模型其他列的真实条件,不惩罚也不进归并
    unparsed     引用过原文校验但无法解析 → 弱噪声
    suspect      解析成功却在任何途经模型条件集中找不到 → 可疑内容,惩罚置信
    quote_fail   原文校验失败(含空引用)→ 幻觉
    """
    from lineage.core import fingerprint as _fp
    f1_fps = {c["fp"]: c for c in t["conditions"] if not c.get("is_pure_key")}
    all_model_fps = set()
    for mo in hops_by_model:
        for c in graph["models"].get(mo, {}).get("conditions", []):
            all_model_fps.add(c["fp"])
    f2_fps = set()
    agg = {"out_of_scope": [], "unparsed": [], "suspect": [], "quote_fail": 0}
    for out in hops_by_model.values():
        for f in out.get("filters", []):
            if not f.get("quote_verified"):
                f["match"] = "quote_fail"
                agg["quote_fail"] += 1
                continue
            q = re.sub(r"^\s*(qualify|where|having|and|on)\s+", "", f["quote"].strip(), flags=re.I)
            if re.match(r"^using\s*\(", q, flags=re.I):
                f["match"] = "pure_key"       # using 关联键,非业务条件
                continue
            try:
                cond = sqlglot.parse_one(q, read=DIALECT)
            except Exception:
                f["match"] = "unparsed"
                agg["unparsed"].append(q[:80])
                continue
            pieces = list(cond.flatten()) if isinstance(cond, sqlglot.exp.And) else [cond]
            kinds = set()
            for piece in pieces:
                if (isinstance(piece, sqlglot.exp.EQ)
                        and isinstance(piece.this, sqlglot.exp.Column)
                        and isinstance(piece.expression, sqlglot.exp.Column)):
                    kinds.add("pure_key")     # a.x = b.x,不计入业务条件差异
                    continue
                fp = _fp(normalize_condition(piece))
                if fp in f1_fps:
                    f2_fps.add(fp)
                    kinds.add("matched")
                elif fp in all_model_fps:
                    kinds.add("out_of_scope")
                    agg["out_of_scope"].append(piece.sql(dialect=DIALECT)[:80])
                else:
                    kinds.add("suspect")
                    agg["suspect"].append(piece.sql(dialect=DIALECT)[:80])
            # 复合条件取最保守标注:含可疑片段即 suspect;否则有匹配片段才可进归并
            f["match"] = next((k for k in ("suspect", "matched", "out_of_scope", "pure_key")
                               if k in kinds), "pure_key")
    return {"f1_fps": f1_fps, "f2_fps": f2_fps, **agg}


def cross_validate(t: dict, hops_by_model: dict, cls: dict) -> dict:
    s1 = {f"{s['table']}.{s['column']}" for s in t["sources"]}
    s2 = set()
    for out in hops_by_model.values():
        for cinfo in out.get("columns", {}).values():
            for sc in cinfo.get("source_columns", []):
                tbl = str(sc.get("table", "")).split(".")[-1].strip('"')
                if tbl.startswith("ods_"):
                    s2.add(f"{tbl}.{sc.get('column')}")
    f1_fps, f2_fps = cls["f1_fps"], cls["f2_fps"]
    f2_out_of_scope, f2_unparsed = cls["out_of_scope"], cls["unparsed"]
    f2_suspect, quote_fail = cls["suspect"], cls["quote_fail"]
    covered = len(f2_fps) / len(f1_fps) if f1_fps else 1.0
    missing = sorted(s1 - s2)
    # 条件列豁免:LLM 多报的字段若真实出现在通道一的过滤/语义文本中,属"值链 vs 条件列"定义分歧,不算幻觉
    cond_text = norm_text(" ".join(c["sql"] for c in t["conditions"])
                          + " ".join(x.get("sql", "") for x in t["semantics"]))
    extra_all = sorted(s2 - s1)
    extra = [e for e in extra_all if e.rsplit(".", 1)[-1] not in cond_text]
    cond_cols = [e for e in extra_all if e not in extra]
    if not missing and not extra and covered >= 0.999 and quote_fail == 0 and not f2_suspect:
        conf = "high"
    elif not missing and len(extra) <= 1 and covered >= 0.8 and quote_fail == 0 and len(f2_suspect) <= 1:
        conf = "medium"
    else:
        conf = "low"
    return {
        "confidence": conf,
        "s_missing_by_llm": missing, "s_extra_by_llm": extra,
        "s_condition_cols_by_llm": cond_cols,
        "f1_total": len(f1_fps), "f1_covered": round(covered, 3),
        "f1_uncovered": [f1_fps[fp]["sql"][:80] for fp in f1_fps if fp not in f2_fps],
        "f2_out_of_scope": f2_out_of_scope[:6], "f2_unparsed": f2_unparsed[:6],
        "f2_suspect": f2_suspect[:6], "quote_verify_fail": quote_fail,
    }


# ---------------- 单指标流水线 ----------------
def run_metric(m: dict, graph: dict, docs: dict, lexicon: dict, dup_pairs: list | None = None) -> dict:
    model, col = m["target"].rsplit(".", 1)
    t = trace(graph, model, col)
    for et in m.get("extra_targets", []):
        em, ec = et.rsplit(".", 1)
        t2 = trace(graph, em, ec)
        t["conditions"] += [c for c in t2["conditions"] if c["fp"] not in {x["fp"] for x in t["conditions"]}]
        t["semantics"] += [s for s in t2["semantics"] if s not in t["semantics"]]
        t["expr_chain"] += [e for e in t2["expr_chain"] if e not in t["expr_chain"]]
        for s in t2["sources"]:
            if s not in t["sources"]:
                t["sources"].append(s)
        for vm in t2["models_visited"]:
            if vm not in t["models_visited"]:
                t["models_visited"].append(vm)

    # 通道二:逐跳(同模型合并一次调用;输入只有该模型 SQL,与通道一独立)
    cols_by_model = {}
    for e in t["expr_chain"]:
        cols_by_model.setdefault(e["model"], set()).add(e["column"])
    hops_by_model = {}
    with ThreadPoolExecutor(max_workers=4) as hex_:
        futs = {}
        for mo, cols in cols_by_model.items():
            info = graph["models"][mo]
            sql = (ROOT / info["compiled_path"]).read_text()
            futs[hex_.submit(extract_hop, mo, sql, sorted(cols), info["layer"])] = mo
        for fut, mo in futs.items():
            hops_by_model[mo] = fut.result()

    cls = classify_filters(t, hops_by_model, graph)
    val = cross_validate(t, hops_by_model, cls)

    order = {mm: i for i, mm in enumerate(t["models_visited"])}
    hops_seq = [{"model": mo, "layer": graph["models"][mo]["layer"], **hops_by_model[mo]}
                for mo in sorted(hops_by_model, key=lambda x: order.get(x, 99))]
    # 归并输入只保留与本指标相关且通过机器校验的条件(P1-B:
    # out_of_scope / unparsed / suspect / quote_fail 一律不得进入卡片内容)
    merge_input = [{"model": h["model"], "layer": h["layer"], "columns": h.get("columns", {}),
                    "filters": [{"quote": f["quote"], "kind": f.get("kind"), "effect": f.get("effect")}
                                for f in h.get("filters", []) if f.get("match") == "matched"]}
                   for h in hops_seq]
    technical = merge_hops(m["title"], m["target"], merge_input)
    if m.get("query_filter"):
        technical.setdefault("key_filters", []).append({"text": m["query_filter"], "layer": "查询层"})

    # 业务口径上下文:目标列 + 源字段 + 途经列的注释
    docs_ctx = {}
    for s in t["sources"]:
        d = docs.get(s["table"], {}).get(s["column"])
        if d:
            docs_ctx[f"{s['table']}.{s['column']}"] = d
    for e in t["expr_chain"]:
        d = docs.get(e["model"], {}).get(e["column"])
        if d:
            docs_ctx[f"{e['model']}.{e['column']}"] = d
    evidence = build_evidence(t, hops_seq)
    business = gen_business(m["title"], technical, docs_ctx, lexicon, m.get("query_filter"), evidence)

    # 条款证据绑定(P1-B):每条业务条款必须引用有效证据 ID;证据只来自血缘/AST/已验证原文,
    # 不含 LLM 归并产物。任一无法绑定证据的条款 → 该卡不得进入 high。
    ev_by_id = {e["id"]: e for e in evidence}
    unverified = 0
    for c in business.get("clauses", []):
        ids = c.get("evidence_ids") or []
        ok = bool(ids) and all(i in ev_by_id for i in ids)
        if ok:
            cited = norm_text(" ".join(ev_by_id[i]["text"] for i in ids))
            basis = norm_text(str(c.get("basis", "")))
            toks = [tk for tk in re.findall(r"[a-z_][a-z0-9_]{2,}|\d{2,}", basis)
                    if tk not in ("case", "when", "then", "else", "end", "and", "sum",
                                  "select", "from", "where", "null", "not", "the")]
            if not basis:
                ok = False                                  # basis 必须照抄所引证据
            elif toks:
                ok = sum(1 for tk in set(toks) if tk in cited) / len(set(toks)) >= 0.55
            else:
                ok = basis[:40] in cited or cited[:40] in basis
        c["basis_verified"] = ok
        unverified += 0 if ok else 1
    val["unverified_clauses"] = unverified
    if unverified and val["confidence"] == "high":
        val["confidence"] = "medium"

    # 治理提示:指纹重复对(A 档)命中本卡链路时挂告示。
    # 匹配粒度 = 同模型 + 同基名(refund_amt_14d 与其兄弟列 refund_amt_total 视为同主题)
    from lineage.governance_scan import base as _colbase
    chain_pairs = set()
    for tc in (m["target"], *m.get("extra_targets", [])):
        cm, cc = tc.rsplit(".", 1)
        chain_pairs.add((cm, _colbase(cc)))
    chain_pairs |= {(e["model"], _colbase(e["column"])) for e in t["expr_chain"]}

    def _related(side: str) -> bool:
        sm, sc = side.rsplit(".", 1)
        return (sm, _colbase(sc)) in chain_pairs

    gov_dups = [p for p in (dup_pairs or []) if _related(p["a"]) or _related(p["b"])]

    return {
        "metric_key": m["key"], "title": m["title"], "target": m["target"],
        "run_id": m.get("_run_id"),
        "extra_targets": m.get("extra_targets", []), "query_filter": m.get("query_filter"),
        "generated_at": datetime.now().isoformat(timespec="seconds"), "llm_model": f"{fast_model()}+{quality_model()}",
        "confidence": val["confidence"],
        "status": "published" if val["confidence"] in ("high", "medium") else "review",
        "validation": val, "technical": technical, "business": business,
        "evidence": evidence,
        "governance": {"duplicates": gov_dups[:4]},
        "trace": {k: t[k] for k in ("depth", "models_visited", "sources", "conditions", "semantics")},
        "per_hop": hops_seq,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    args = ap.parse_args()
    run_id = uuid.uuid4().hex[:8]
    graph = load_graph()
    docs = load_column_docs()
    lexicon = yaml.safe_load(open(ROOT / "caliber" / "lexicon.yml"))
    from lineage.governance_scan import scan as gov_scan
    dup_pairs = gov_scan(graph)["duplicates"]
    todo = [dict(m, _run_id=run_id) for m in METRICS if not args.only or m["key"] == args.only]
    if args.only and not todo:
        print(f"未知指标 key: {args.only}", file=sys.stderr)
        sys.exit(2)
    # P1-A:整批写入 runs/<run_id>/,全部成功后才原子切换 active 指针;
    # 中途失败不发布,线上读到的永远是上一个完整批次
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results, failed = {}, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(run_metric, m, graph, docs, lexicon, dup_pairs): m for m in todo}
        for fut, m in futs.items():
            try:
                r = fut.result()
                results[m["key"]] = r
                (run_dir / f"{m['key']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
                v = r["validation"]
                print(f"  ✓ {m['key']:<26} conf={r['confidence']:<6} F覆盖 {v['f1_covered']:.0%}"
                      f"  S漏/多 {len(v['s_missing_by_llm'])}/{len(v['s_extra_by_llm'])}"
                      f"  可疑 {len(v.get('f2_suspect', []))}  未证条款 {v.get('unverified_clauses', 0)}")
            except Exception as e:
                failed.append(m["key"])
                print(f"  ✗ {m['key']}: {e}")
    if failed:
        print(f"失败指标: {failed} —— 本批次不发布,active 指针不动(残留 {run_dir} 供排查)", file=sys.stderr)
        sys.exit(1)
    # --only 时从当前 active 批次补齐其余卡,保证每个发布批次都是完整集合
    if args.only:
        src = active_dir()
        if src is None:
            print("--only 需要已有 active 批次来补齐其余卡;请先跑一次全量", file=sys.stderr)
            sys.exit(1)
        for f in src.glob("*.json"):
            if f.name != "index.json" and not (run_dir / f.name).exists():
                (run_dir / f.name).write_text(f.read_text())
    cards = {}
    for f in sorted(run_dir.glob("*.json")):
        if f.name == "index.json":
            continue
        r = json.loads(f.read_text())
        cards[r["metric_key"]] = {"title": r["title"], "confidence": r["confidence"],
                                  "status": r["status"], "generated_at": r["generated_at"],
                                  "run_id": r.get("run_id")}
    at = datetime.now().isoformat(timespec="seconds")
    idx = {"run_id": run_id, "at": at, "cards": cards,
           "requested": len(todo), "succeeded": len(results),
           "mode": f"only:{args.only}" if args.only else "full"}
    (run_dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1))
    activate(run_id, {"at": at})
    # 清理:历史批次只保留最近 3 个;顶层旧平铺卡片已由 runs/ 机制取代
    runs_sorted = sorted((d for d in RUNS.iterdir() if d.is_dir()),
                         key=lambda d: d.stat().st_mtime, reverse=True)
    for d in runs_sorted[3:]:
        if d.name != run_id:
            shutil.rmtree(d, ignore_errors=True)
    for f in STORE.glob("*.json"):
        f.unlink()
    print(f"\nstore: {len(results)}/{len(todo)} 张生成,批次 {run_id} 已发布并激活 → caliber/store/runs/{run_id}/")


if __name__ == "__main__":
    main()
