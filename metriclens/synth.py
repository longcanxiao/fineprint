#!/usr/bin/env python3
"""口径合成流水线:双通道互验 + 技术/业务口径生成 + 批次化口径知识库。

通道一 = 血缘引擎的确定性 S₁/F₁/E₁(metriclens.trace);
通道二 = LLM 逐跳解析单模型 SQL(独立输入,不喂通道一结果),原文引用机器校验防幻觉;
归并输入只保留互验 matched 的条件;业务条款必须绑定确定性证据 ID;
互验分歧 → 置信分级,低置信进审核队列不对外展示。
"""
import json
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import sqlglot

from metriclens import prompts
from metriclens.config import MetricDef, MLConfig
from metriclens.governance import agg_signature
from metriclens.governance import base as colbase
from metriclens.governance import scan as governance_scan
from metriclens.lineage import dialect, fingerprint, normalize_condition
from metriclens.llm import chat_json, fast_model, quality_model, set_cache_dir
from metriclens.project import DbtProject
from metriclens.render import (attach_evidence, build_facts, formula_authority,
                               publication_status, race_formula)
from metriclens.store import CaliberStore
from metriclens.trace import display_name, resolve_model, trace


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).replace('"', "").lower()).strip()


# ---------------- LLM 输出结构校验 ----------------
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


# ---------------- 自由文本词表校验 ----------------
_FREETEXT_IDENT = re.compile(r"\b[a-z][a-z0-9]*(?:[._][a-z0-9]+)+\b")
_FREETEXT_NUM = re.compile(r"\b\d{2,}(?:\.\d+)?\b")
# 时间窗口数字:一位数也校验(限 7 天/1 日内是真口径数字,\d{2,} 拦不住篡改)
_FREETEXT_WINNUM = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:天|日|小时|周|个月|months?|days?|hours?|weeks?)")
_NUM_WHITELIST = {"100"}   # 百分比换算常数,散文公式常见
_AGG_FN = re.compile(r"\b(sum|count|avg|min|max)\s*\(\s*(distinct\b)?", re.I)
_AGG_ROWCOUNT = re.compile(r"\b(?:count\s*\(\s*(?:\*|1)\s*\)|sum\s*\(\s*1\s*\))", re.I)


def build_vocab(t: dict, title: str, query_filter, lexicon: dict, graph: dict | None = None) -> tuple:
    """通道一确定性词表(标识符集 + 数字集):公式/定义/告诫等自由文本里的
    复合标识符(snake_case/带点引用)与口径数字必须能在这里找到出处。
    词表不含任何 LLM 产物,也不含 schema 文档——第三方 dbt 包的注释属不可信输入,
    不得为卡片表述背书;lexicon 是用户在 metriclens.yml 亲手维护的一方配置,保留。
    graph=None 时是"链内词表"(只含本指标值链对象),供公式校验——公式写了项目里
    真实存在但不在本链的列就是错误公式,必须拦;传 graph 时并入全图模型/列/源表名,
    供摘要/告诫校验——对比说明引用真实对象不是词法幻觉,不拦。口径数字始终限
    本指标链路——窗口/状态码不得跨指标借用。"""
    parts = [title or "", query_filter or ""]
    parts += [f"{e['model']} {e['column']} {e.get('expr') or ''}" for e in t["expr_chain"]]
    parts += [f"{s.get('schema', '')} {s['table']} {s['column']}" for s in t["sources"]]
    parts += [c["sql"] for c in t["conditions"]]
    parts += [str(s.get("sql") or "") for s in t["semantics"]]
    parts += list(t["models_visited"])
    parts += [f"{k} {v}" for k, v in (lexicon or {}).items()]
    text = norm_text(" ".join(parts))
    idents = set()
    for tk in _FREETEXT_IDENT.findall(text):
        idents.add(tk)
        idents.update(tk.split("."))   # 别名点引用(o.is_test_account)按段入表:卡片常写裸列名
    nums = set(re.findall(r"\b\d+(?:\.\d+)?\b", text)) | _NUM_WHITELIST   # 词表侧宽收,含一位数
    for uid, m in (graph or {}).get("models", {}).items():
        idents.add(uid)
        idents.update(uid.lower().split("."))       # unique_id 各段(含短名)均可溯
        if m.get("name"):
            idents.add(str(m["name"]).lower())
        idents.update(c.lower() for c in m.get("columns", {}))
        for tb in m.get("row_set_tables") or []:
            idents.add(str(tb).lower())
            idents.update(str(tb).lower().split("."))   # 三段物理键按段入表,卡片写裸名可溯
    for rel, ident in (graph or {}).get("relations", {}).get("sources", {}).items():
        idents.update((str(rel).lower(), str(ident).lower()))
        idents.update(str(rel).lower().split("."))
    return idents, nums


def verify_freetext(text, idents: set, nums: set) -> list:
    """返回文本中无法溯源的 token;空列表 = 通过。散文普通单词(无下划线/点)
    不校验——zh/en 文本都只拦"看起来像字段/表引用的词"和"口径数字"。"""
    s = norm_text(str(text or ""))

    def ok(tk: str) -> bool:
        if tk in idents:
            return True
        # model.column 式点引用:拆段判定,具备标识符特征(含下划线)的段必须可溯源
        return all(p in idents or "_" not in p for p in tk.split("."))

    bad = [tk for tk in _FREETEXT_IDENT.findall(s) if not ok(tk)]
    bad += [tk for tk in _FREETEXT_NUM.findall(s) if tk not in nums]
    bad += [tk for tk in _FREETEXT_WINNUM.findall(s) if tk not in nums and tk not in bad]
    return bad


def formula_agg_check(formula, link_aggs: set) -> list:
    """公式聚合一致性(语义锚点):formula 声称的聚合语义必须与通道一表达式链的
    聚合签名一致——纯散文公式(链路有聚合而公式一个都没写)或凭空多出的聚合都拦。
    链路签名为空 = 证据不可见,不下结论(与治理同原则)。avg 与 sum/count 互为
    展开形,豁免。返回失配描述列表;空 = 通过。"""
    if not link_aggs:
        return []
    text = norm_text(str(formula or ""))
    f_aggs = set()
    if _AGG_ROWCOUNT.search(text):
        f_aggs.add("rowcount")
        text = _AGG_ROWCOUNT.sub(" ", text)   # 剔除行数形,剩余文本按普通聚合抓取
    f_aggs |= {fn.lower() + (":distinct" if dist else "") for fn, dist in _AGG_FN.findall(text)}
    if not f_aggs:
        return [f"公式未表达任何聚合(链路聚合: {sorted(link_aggs)})"]
    extra = f_aggs - set(link_aggs)
    if "avg" in extra and {"sum", "count"} <= set(link_aggs):
        extra.discard("avg")   # avg = sum/count 展开形
    return [f"公式聚合 {sorted(extra)} 不在链路聚合 {sorted(link_aggs)} 中"] if extra else []


# ---------------- 通道二:逐跳抽取 ----------------
def verify_quotes(out: dict, sql: str) -> dict:
    """原文引用机器校验(纯函数):空引用与不在原文中的引用一律判失败。"""
    nsql = norm_text(sql)
    for f in out.get("filters", []):
        nq = norm_text(f.get("quote", ""))
        f["quote_verified"] = bool(nq) and nq in nsql
    return out


def extract_hop(lang: str, model_name: str, sql: str, cols: list, layer: str) -> dict:
    user = f"模型: {model_name}(分层 {layer})\n目标输出列: {', '.join(cols)}\n\nSQL:\n```sql\n{sql}\n```"
    out = chat_json(prompts.HOP[lang], user, max_tokens=3000, model=fast_model(), validator=validate_hop)
    return verify_quotes(out, sql)


def merge_hops(lang: str, title: str, target: str, hops: list) -> dict:
    user = f"指标: {title}(目标列 {target})\n逐跳口径(自消费层向源):\n{json.dumps(hops, ensure_ascii=False, indent=1)}"
    return chat_json(prompts.MERGE[lang], user, max_tokens=8000, model=quality_model(), validator=validate_merge)


def build_evidence(t: dict, hops_seq: list) -> list:
    """确定性证据清单:通道一条件(E)/语义点(S)/表达式链(X)+ 已互验匹配的原文引用(Q)。
    不含任何 LLM 归并产物,杜绝「LLM 引用 LLM」循环证据。"""
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


def gen_business(lang: str, title: str, technical: dict, docs_ctx: dict, lexicon: dict,
                 query_filter: str | None, evidence: list) -> dict:
    ev_lines = "\n".join(f"[{e['id']}] ({e['kind']}, {e.get('model') or '-'}) {e['text']}" for e in evidence)
    user = (f"指标: {title}\n"
            + (f"取数过滤: {query_filter}\n" if query_filter else "")
            + f"技术口径:\n{json.dumps(technical, ensure_ascii=False, indent=1)}\n\n"
            f"证据清单(clauses 只能引用以下编号):\n{ev_lines}\n\n"
            f"相关字段业务注释:\n{json.dumps(docs_ctx, ensure_ascii=False, indent=1)}\n\n"
            f"业务词典:\n{json.dumps(lexicon, ensure_ascii=False, indent=1)}")
    return chat_json(prompts.BIZ[lang], user, max_tokens=8000, model=quality_model(), validator=validate_biz)


# ---------------- 互验 ----------------
def classify_filters(t: dict, hops_by_model: dict, graph: dict) -> dict:
    """给通道二每条 filter 打相关性标注(match 字段),并返回聚合计数。

    matched      与通道一 scope 内条件指纹一致 → 本指标真实口径,可进归并与证据池
    pure_key     纯关联键(a.x=b.x / using),无业务语义
    out_of_scope 指纹在途经模型全条件集但不在本指标 scope → 其他列的真实条件,不惩罚也不进归并
    unparsed     引用过原文校验但无法解析 → 弱噪声
    suspect      解析成功却在任何途经模型条件集中找不到 → 可疑内容,惩罚置信
    quote_fail   原文校验失败(含空引用)→ 幻觉
    """
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
                f["match"] = "pure_key"
                continue
            try:
                cond = sqlglot.parse_one(q, read=dialect())
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
                    kinds.add("pure_key")
                    continue
                fp = fingerprint(normalize_condition(piece))
                if fp in f1_fps:
                    f2_fps.add(fp)
                    kinds.add("matched")
                elif fp in all_model_fps:
                    kinds.add("out_of_scope")
                    agg["out_of_scope"].append(piece.sql(dialect=dialect())[:80])
                else:
                    kinds.add("suspect")
                    agg["suspect"].append(piece.sql(dialect=dialect())[:80])
            f["match"] = next((k for k in ("suspect", "matched", "out_of_scope", "pure_key")
                               if k in kinds), "pure_key")
    return {"f1_fps": f1_fps, "f2_fps": f2_fps, **agg}


def _src_ident(s: dict) -> str:
    """源表文档键:schema.table(schema 缺失时裸名)。供 column_docs 查询。"""
    sch = s.get("schema") or ""
    return f"{sch}.{s['table']}" if sch else s["table"]


def _src_ident3(s: dict) -> str:
    """源表互验身份:物理三段 db.schema.table(缺段留空位)。
    跨 schema、跨 database 的同名表都是不同数据,互验不得降级比对。"""
    return f"{s.get('database') or ''}.{s.get('schema') or ''}.{s['table']}"


def cross_validate(t: dict, hops_by_model: dict, cls: dict, source_names: set,
                   model_tables: set | None = None) -> dict:
    # 通道一溯到的叶子表并入源表集:seed / 未在 dbt sources 声明的表(如 jaffle_shop)
    # 也是合法源,否则 s2 永远对不上;保留传入集合以捕捉 LLM 报链路外真实源表的情况
    idents3 = {_src_ident3(s) for s in t["sources"]}
    by2: dict = {}     # 'schema.table' → {三段身份}:两段指认按此对齐
    bare_index: dict = {}
    for s in t["sources"]:
        i3 = _src_ident3(s)
        sch = s.get("schema") or ""
        by2.setdefault(f"{sch}.{s['table']}" if sch else s["table"], set()).add(i3)
        bare_index.setdefault(s["table"], set()).add(i3)
    legal = set(source_names) | idents3 | set(by2) | set(bare_index)
    # 表级源(column="*",COUNT(*) 类行集依赖):LLM 指认该表任意列即视为命中
    star_tables = {_src_ident3(s) for s in t["sources"] if s["column"] == "*"}
    s1 = {f"{_src_ident3(s)}.{s['column']}" for s in t["sources"] if s["column"] != "*"}

    mtabs = model_tables or set()

    def resolve(raw: str) -> str | None:
        """LLM 报的表名 → 通道一源表三段身份。对齐规则:逐段比对,某一侧未知
        (空段/未写)则该段宽容,两侧都声明则必须一致——显式的 schema/database
        指认不得被回退改写:database 错配、伪造 schema 都保留原样进 extra
        (真身份同时落 missing,双向惩罚);多候选歧义不猜(进 missing)。
        图内模型表是通道二逐跳的合法指认对象(上游模型),非源级,静默滤除。"""
        tb = raw.replace('"', "").strip()
        if not tb:
            return None
        parts = tb.split(".")
        d = parts[-3] if len(parts) >= 3 else None       # None = LLM 未声明该段
        sch = parts[-2] if len(parts) >= 2 else None
        tail = parts[-1]

        def compat(cand: str) -> bool:
            cdb, csch, _ = cand.split(".", 2)
            return ((d is None or cdb in ("", d))
                    and (sch is None or csch in ("", sch)))

        if len(parts) >= 3 and f"{d}.{sch}.{tail}" in idents3:
            return f"{d}.{sch}.{tail}"
        hits = [c for c in sorted(bare_index.get(tail, ())) if compat(c)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None          # 多候选按声明段仍无法定位 → 诚实进 missing
        if tb in mtabs or tail in mtabs or (sch is not None and f"{sch}.{tail}" in mtabs):
            return None          # 中间模型指认:合法但非源,不入 s2 不惩罚
        if d is not None:
            return f"{d}.{sch}.{tail}"   # 显式三段但链上无兼容源:伪身份,extra+missing
        if sch is not None:
            return tb            # 显式两段(含伪造/链路外 schema):不尾段回退,进 extra
        return tb if tb in legal else None

    s2 = set()
    for out in hops_by_model.values():
        for cinfo in out.get("columns", {}).values():
            for sc in cinfo.get("source_columns", []):
                ident = resolve(str(sc.get("table", "")))
                if ident:
                    s2.add(f"{ident}.{sc.get('column')}")
    s2_tables = {e.rsplit(".", 1)[0] for e in s2}
    f1_fps, f2_fps = cls["f1_fps"], cls["f2_fps"]
    covered = len(f2_fps) / len(f1_fps) if f1_fps else 1.0
    missing = sorted(s1 - s2) + sorted(f"{tb}.*" for tb in star_tables if tb not in s2_tables)
    # star 表的具体列通道一无从对证,不参与 extra 判定;s1 已声明的具体列保留
    s2 = (s1 & s2) | {e for e in s2 if e.rsplit(".", 1)[0] not in star_tables}
    # 条件列豁免:LLM 多报的字段若真实出现在通道一的过滤/语义文本中,属"值链 vs 条件列"定义分歧,不算幻觉
    cond_text = norm_text(" ".join(c["sql"] for c in t["conditions"])
                          + " ".join(x.get("sql", "") for x in t["semantics"]))
    extra_all = sorted(s2 - s1)
    extra = [e for e in extra_all if e.rsplit(".", 1)[-1] not in cond_text]
    cond_cols = [e for e in extra_all if e not in extra]

    # 上下文豁免:引用落在 join/分组上下文表(通道一第二类视野)= 解释口径的
    # 合法引用而非值源幻觉,单独记账不惩罚。模型上下文带列清单,列不存在不豁免
    # (幻觉信号保留);真实表上下文列不可知,按表身份豁免(段级宽容同 resolve)。
    def _ctx_hit(entry: str) -> bool:
        tb, col = entry.rsplit(".", 1)
        parts = tb.replace('"', "").split(".")
        d = parts[-3] if len(parts) >= 3 else None
        sch = parts[-2] if len(parts) >= 2 else None
        tail = parts[-1]
        for c in t.get("context_tables") or []:
            crel = str(c["table"])
            cd, csch, ctail = crel.split(".", 2) if crel.count(".") >= 2 else ("", "", crel)
            if tail not in (ctail, c.get("model")):
                continue
            if d is not None and cd not in ("", d):
                continue
            if sch is not None and csch not in ("", sch):
                continue
            cols = c.get("columns")
            if cols is not None and col.lower() not in cols:
                continue
            return True
        return False

    ctx_cols = [e for e in extra if _ctx_hit(e)]
    extra = [e for e in extra if e not in ctx_cols]
    quote_fail, f2_suspect = cls["quote_fail"], cls["suspect"]
    if not missing and not extra and covered >= 0.999 and quote_fail == 0 and not f2_suspect:
        conf = "high"
    elif not missing and len(extra) <= 1 and covered >= 0.8 and quote_fail == 0 and len(f2_suspect) <= 1:
        conf = "medium"
    else:
        conf = "low"
    return {
        "confidence": conf,
        "s_missing_by_llm": missing, "s_extra_by_llm": extra,
        "s_condition_cols_by_llm": cond_cols, "s_context_by_llm": ctx_cols,
        "f1_total": len(f1_fps), "f1_covered": round(covered, 3),
        "f1_uncovered": [f1_fps[fp]["sql"][:80] for fp in f1_fps if fp not in f2_fps],
        "f2_out_of_scope": cls["out_of_scope"][:6], "f2_unparsed": cls["unparsed"][:6],
        "f2_suspect": f2_suspect[:6], "quote_verify_fail": quote_fail,
    }


# ---------------- 单指标流水线 ----------------
def target_exposures(graph: dict, target_uids) -> list:
    """指标的消费方 = 挂在其目标模型(出口层)上的 dbt exposures。
    只看目标模型不看途经模型:exposure 消费的是被依赖的那张物化表,
    上游模型的指标不因下游被看板引用而蹭消费方。"""
    out = []
    for uid in target_uids:
        for e in (graph.get("exposures_by_model") or {}).get(uid, []):
            if e not in out:
                out.append(e)
    return out


def merged_trace(graph: dict, m: MetricDef) -> dict:
    model, col = m.target.rsplit(".", 1)
    t = trace(graph, model, col)
    for et in m.extra_targets:
        em, ec = et.rsplit(".", 1)
        t2 = trace(graph, em, ec)
        seen_fp = {c["fp"] for c in t["conditions"]}
        t["conditions"] += [c for c in t2["conditions"] if c["fp"] not in seen_fp]
        t["semantics"] += [s for s in t2["semantics"] if s not in t["semantics"]]
        t["expr_chain"] += [e for e in t2["expr_chain"] if e not in t["expr_chain"]]
        t["sources"] += [s for s in t2["sources"] if s not in t["sources"]]
        t["scope_ambiguous"] += [x for x in t2.get("scope_ambiguous", [])
                                 if x not in t["scope_ambiguous"]]
        seen_ctx = {c["table"] for c in t.get("context_tables") or []}
        t["context_tables"] = (t.get("context_tables") or []) + [
            c for c in t2.get("context_tables") or [] if c["table"] not in seen_ctx]
        for vm in t2["models_visited"]:
            if vm not in t["models_visited"]:
                t["models_visited"].append(vm)
    return t


def run_metric(project: DbtProject, cfg: MLConfig, graph: dict, m: MetricDef,
               dup_pairs: list, run_id: str) -> dict:
    lang = cfg.language
    t = merged_trace(graph, m)

    # 通道二:逐跳(同模型合并一次调用;输入只有该模型 SQL,与通道一独立)
    # hops 以 model_uid 为键(graph 主键);LLM 提示词与产出用展示名
    cols_by_model, disp_of = {}, {}
    for e in t["expr_chain"]:
        cols_by_model.setdefault(e["model_uid"], set()).add(e["column"])
        disp_of[e["model_uid"]] = e["model"]
    hops_by_model = {}
    with ThreadPoolExecutor(max_workers=4) as hex_:
        futs = {}
        for mo, cols in cols_by_model.items():
            info = graph["models"][mo]
            sql = (project.project_dir / info["compiled_path"]).read_text()
            futs[hex_.submit(extract_hop, lang, disp_of[mo], sql, sorted(cols), info["layer"])] = mo
        for fut, mo in futs.items():
            hops_by_model[mo] = fut.result()

    cls = classify_filters(t, hops_by_model, graph)
    rel_sources = graph.get("relations", {}).get("sources", {})
    # 合法源身份三种粒度:裸名 + schema.table 两段(LLM 常用)+ 物理三段键
    source_names = set(rel_sources.values()) | set(rel_sources.keys())
    source_names |= {k.split(".", 1)[1] for k in rel_sources if k.count(".") >= 2}
    # 图内模型表名(三段/两段/裸名/短名):通道二逐跳的合法上游指认,非源级
    model_tables = set()
    for k in graph.get("relations", {}).get("models", {}):
        model_tables.add(k)
        ps = k.split(".")
        model_tables.add(".".join(ps[1:]))
        model_tables.add(ps[-1])
    for mm in graph["models"].values():
        if mm.get("name"):
            model_tables.add(mm["name"])
    val = cross_validate(t, hops_by_model, cls, source_names, model_tables)

    order = {mm: i for i, mm in enumerate(t["models_visited"])}
    hops_seq = [{"model": disp_of.get(mo, mo), "layer": graph["models"][mo]["layer"],
                 **hops_by_model[mo]}
                for mo in sorted(hops_by_model, key=lambda x: order.get(x, 99))]
    # 归并输入只保留与本指标相关且通过机器校验的条件
    merge_input = [{"model": h["model"], "layer": h["layer"], "columns": h.get("columns", {}),
                    "filters": [{"quote": f["quote"], "kind": f.get("kind"), "effect": f.get("effect")}
                                for f in h.get("filters", []) if f.get("match") == "matched"]}
                   for h in hops_seq]
    technical = merge_hops(lang, m.title, m.target, merge_input)
    if m.query_filter:
        technical.setdefault("key_filters", []).append({"text": m.query_filter, "layer": "query"})

    docs = project.column_docs
    docs_ctx = {}
    for s in t["sources"]:
        # 全名 → schema 名 → 裸名逐级回退:同名源表的注释不得互相串
        d = (docs.get(_src_ident3(s)) or docs.get(_src_ident(s))
             or docs.get(s["table"]) or {}).get(s["column"])
        if d:
            docs_ctx[f"{s['table']}.{s['column']}"] = d
    for e in t["expr_chain"]:
        d = docs.get(e["model"], {}).get(e["column"])
        if d:
            docs_ctx[f"{e['model']}.{e['column']}"] = d

    evidence = build_evidence(t, hops_seq)
    business = gen_business(lang, m.title, technical, docs_ctx, cfg.lexicon, m.query_filter, evidence)

    # 条款证据绑定:每条业务条款必须引用有效证据 ID;任一未绑定 → 该卡不得 high
    ev_by_id = {e["id"]: e for e in evidence}
    unverified = 0
    for c in business.get("clauses", []):
        ids = c.get("evidence_ids") or []
        ok = bool(ids) and all(i in ev_by_id for i in ids)
        if ok:
            # cited 须与 gen_business 提示词中的证据行渲染一致(含编号/类型/模型),
            # LLM 连同元数据前缀一起忠实抄写时不应被误判为未证
            cited = norm_text(" ".join(
                f"[{i}] ({ev_by_id[i]['kind']}, {ev_by_id[i].get('model') or '-'}) {ev_by_id[i]['text']}"
                for i in ids))
            basis = norm_text(str(c.get("basis", "")))
            toks = [tk for tk in re.findall(r"[a-z_][a-z0-9_]{2,}|\d{2,}", basis)
                    if tk not in ("case", "when", "then", "else", "end", "and", "sum",
                                  "select", "from", "where", "null", "not", "the")]
            if not basis:
                ok = False
            elif toks:
                ok = sum(1 for tk in set(toks) if tk in cited) / len(set(toks)) >= 0.55
            else:
                ok = basis[:40] in cited or cited[:40] in basis
        c["basis_verified"] = ok
        unverified += 0 if ok else 1
    val["unverified_clauses"] = unverified
    if unverified and val["confidence"] == "high":
        val["confidence"] = "medium"
    # 空条款 = 业务口径不含任何可验证事实,绑定机制整体失效,不得 high
    val["empty_clauses"] = not business.get("clauses")
    if val["empty_clauses"] and val["confidence"] == "high":
        val["confidence"] = "medium"

    # 自由文本词表校验:公式/摘要/定义/告诫/关键过滤是展示层最显眼的字段,
    # 其中的字段引用与口径数字必须可溯源到通道一词表;失配 → 该卡不得 high。
    # 词表分两档:公式必须绑定本指标值链(链内词表,不含全图对象——项目里真实存在
    # 但不在本链的列写进公式就是错误公式);摘要/定义/告诫允许引用全图真实对象
    # (对比说明、关联指标是合法表述)。
    v_idents, v_nums = build_vocab(t, m.title, m.query_filter, cfg.lexicon, graph)
    c_idents, c_nums = build_vocab(t, m.title, m.query_filter, cfg.lexicon, None)
    freetext_bad = {}
    # 聚合锚点比较集 = 值链聚合 + 途经模型内全部列的聚合(LLM 公式重构常内联
    # 途经模型的中间取数逻辑,如 sign_time = min(case …);凭空聚合仍在集外被拦)
    link_aggs = set(agg_signature(t))
    for mo in t["models_visited"]:
        link_aggs |= set(graph["models"].get(mo, {}).get("agg_fns") or [])
    agg_bad = formula_agg_check(technical.get("formula"), link_aggs)
    if agg_bad:
        freetext_bad["formula_aggs"] = agg_bad
    checks = [("formula", technical.get("formula"), True), ("summary", technical.get("summary"), False),
              ("definition", business.get("definition"), False)]
    checks += [(f"key_filters[{i}]", kf.get("text"), False)
               for i, kf in enumerate(technical.get("key_filters") or [])]
    checks += [(f"caveats[{i}]", cv, False) for i, cv in enumerate(business.get("caveats") or [])]
    for field, txt, chain_only in checks:
        b = (verify_freetext(txt, c_idents, c_nums) if chain_only
             else verify_freetext(txt, v_idents, v_nums))
        if b:
            freetext_bad[field] = b[:6]
    val["freetext_unverified"] = freetext_bad
    if freetext_bad and val["confidence"] == "high":
        val["confidence"] = "medium"

    # 归因不明条件(别名复用 scope):口径事实不完整,不得 high,须人工确认
    amb = t.get("scope_ambiguous") or []
    if amb:
        val["scope_ambiguous"] = [str(c.get("sql", ""))[:80] for c in amb][:6]
        if val["confidence"] == "high":
            val["confidence"] = "medium"

    # 0.8 双写:确定性组合器合成技术公式(逐事实 status+证据),与 LLM 公式
    # 规范化比对。赛马已裁决——公式权威=组合器(authority=machine),LLM 退居
    # 解释与叙述;组合器不可证时 LLM 公式兜底(authority=llm_fallback,按其
    # 全套互验置信裁决)。race 判定保留为叙述层质检信号。组合器任何失败都
    # 不得影响卡片生成(fail-closed 到 unsupported,不抛)。
    targets = []
    for tc in (m.target, *m.extra_targets):
        cm, cc = tc.rsplit(".", 1)
        targets.append((resolve_model(graph, cm), cc))
    try:
        facts = build_facts(project, graph, t, targets, (c_idents, c_nums), link_aggs)
        attach_evidence(facts, evidence)
    except Exception as e:
        facts = {"formula": {"status": "unsupported", "top": None, "defs": [], "inline": None,
                             "rt_failed": False, "evidence": [],
                             "reasons": [f"internal:{type(e).__name__}: {str(e)[:100]}"]},
                 "key_filters": {"status": "ambiguous" if amb else "proven", "items": [],
                                 "ambiguous_items": [], "reasons": []},
                 "sources": {"status": "proven", "items": [], "reasons": []},
                 "window": {"status": "proven", "items": [], "unique_on": {}, "reasons": []},
                 "grain": {"status": "unknown", "keys": [], "reasons": []}}
    race = race_formula(facts, technical.get("formula"),
                        {"formula_aggs": freetext_bad.get("formula_aggs"),
                         "formula_vocab": freetext_bad.get("formula")})
    facts["formula"]["authority"] = formula_authority(facts)
    pub_status = publication_status(val["confidence"], facts, race)
    # 比较用中间形不入卡(inline_cmp 可能含非法嵌套聚合的纯文本形)
    facts["formula"].pop("inline_cmp", None)
    for p in facts["formula"].get("per_target") or []:
        p.pop("inline_cmp", None)

    # 治理提示:指纹重复对命中本卡链路(同模型 + 同基名)时挂告示。
    # 治理报告两侧是展示名,配置 target 也归一到展示名再比对
    chain_pairs = set()
    for tc in (m.target, *m.extra_targets):
        cm, cc = tc.rsplit(".", 1)
        try:
            cm = display_name(graph, resolve_model(graph, cm))
        except KeyError:
            pass
        chain_pairs.add((cm, colbase(cc, cfg.base_suffixes)))
    chain_pairs |= {(e["model"], colbase(e["column"], cfg.base_suffixes)) for e in t["expr_chain"]}

    def _related(side: str) -> bool:
        sm, sc = side.rsplit(".", 1)
        return (sm, colbase(sc, cfg.base_suffixes)) in chain_pairs

    gov_dups = [p for p in (dup_pairs or []) if _related(p["a"]) or _related(p["b"])]

    return {
        "metric_key": m.key, "title": m.title, "target": m.target,
        "run_id": run_id,
        "extra_targets": m.extra_targets, "query_filter": m.query_filter,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "llm_model": f"{fast_model()}+{quality_model()}",
        # 卡片绑定生成它的图:验收/审计据此判断产物是否落后于图(混版本产物不可信)
        "graph_md5": graph.get("meta", {}).get("graph_md5"),
        "graph_generated_at": graph.get("meta", {}).get("generated_at"),
        "confidence": val["confidence"],
        "status": "published" if val["confidence"] in ("high", "medium") else "review",
        "publication_status": pub_status,
        "consumers": target_exposures(graph, [uid for uid, _ in targets]),
        "validation": val, "technical": technical, "business": business,
        "technical_facts": facts, "race": race,
        "evidence": evidence,
        "governance": {"duplicates": gov_dups[:4]},
        "trace": {k: t[k] for k in ("depth", "models_visited", "sources", "conditions",
                                    "semantics", "scope_ambiguous", "context_tables")},
        "per_hop": hops_seq,
    }


# ---------------- 批次执行 ----------------
def run_all(project: DbtProject, cfg: MLConfig, graph: dict, only: str | None = None) -> int:
    """整批合成并原子发布;返回进程退出码(0=发布成功)。"""
    set_cache_dir(project.workspace / "cache")
    store = CaliberStore(project.workspace / "store")
    run_id = uuid.uuid4().hex[:8]
    scan_r = governance_scan(graph, cfg)
    # 卡片治理提示 = 确定重复 + 疑似重复(行结构不同,基数未证):后者带 kind 标记
    dup_pairs = scan_r["duplicates"] + [{**p, "kind": "row_mismatch"}
                                        for p in scan_r.get("row_mismatch", [])]
    todo = [m for m in cfg.metrics if not only or m.key == only]
    if only and not todo:
        print(f"未知指标 key: {only}(配置里有: {[m.key for m in cfg.metrics]})", file=sys.stderr)
        return 2
    run_dir = store.run_dir(run_id)
    results, failed = {}, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(run_metric, project, cfg, graph, m, dup_pairs, run_id): m for m in todo}
        for fut, m in futs.items():
            try:
                r = fut.result()
                results[m.key] = r
                (run_dir / f"{m.key}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
                v = r["validation"]
                print(f"  ✓ {m.key:<26} conf={r['confidence']:<6} F覆盖 {v['f1_covered']:.0%}"
                      f"  S漏/多 {len(v['s_missing_by_llm'])}/{len(v['s_extra_by_llm'])}"
                      f"  可疑 {len(v.get('f2_suspect', []))}  未证条款 {v.get('unverified_clauses', 0)}"
                      f"  词表失配 {len(v.get('freetext_unverified') or {})}"
                      f"  赛马 {r.get('race', {}).get('verdict', '-')}"
                      f"→{r.get('publication_status', '-')}")
            except Exception as e:
                failed.append(m.key)
                print(f"  ✗ {m.key}: {e}")
    if failed:
        print(f"失败指标: {failed} —— 本批次不发布,active 指针不动(残留 {run_dir} 供排查)", file=sys.stderr)
        return 1
    # --only 时从当前 active 批次补齐其余卡,保证每个发布批次都是完整集合
    if only:
        src = store.active_dir()
        if src is None:
            print("--only 需要已有 active 批次来补齐其余卡;请先跑一次全量", file=sys.stderr)
            return 1
        for f in src.glob("*.json"):
            if f.name != "index.json" and not (run_dir / f.name).exists():
                (run_dir / f.name).write_text(f.read_text())
    cards = {}
    race_counts, pub_counts = {}, {}
    disagree_keys, unsup_keys = [], []
    for f in sorted(run_dir.glob("*.json")):
        if f.name == "index.json":
            continue
        r = json.loads(f.read_text())
        cards[r["metric_key"]] = {"title": r["title"], "confidence": r["confidence"],
                                  "status": r["status"], "generated_at": r["generated_at"],
                                  "run_id": r.get("run_id"),
                                  "publication_status": r.get("publication_status"),
                                  "race": (r.get("race") or {}).get("verdict"),
                                  "formula_authority": ((r.get("technical_facts") or {})
                                                        .get("formula") or {}).get("authority")}
        # --only 补齐的旧批次卡可能没有 race 字段(0.8 前),按缺省计入
        rv = (r.get("race") or {}).get("verdict") or "-"
        race_counts[rv] = race_counts.get(rv, 0) + 1
        pv = r.get("publication_status") or "-"
        pub_counts[pv] = pub_counts.get(pv, 0) + 1
        if rv == "disagree":
            disagree_keys.append(r["metric_key"])
        if rv == "renderer_unsupported":
            unsup_keys.append(r["metric_key"])
    # 发布前完整性断言:批次卡片集合必须与配置指标集合一致(缺失或多余都不发布)
    want = {m.key for m in cfg.metrics}
    if set(cards) != want:
        print(f"批次不完整,不发布: 缺 {sorted(want - set(cards))} 多 {sorted(set(cards) - want)}"
              + ("(--only 补齐依赖的 active 批次与当前配置不一致,请先跑一次全量)" if only else ""),
              file=sys.stderr)
        return 1
    at = datetime.now().isoformat(timespec="seconds")
    idx = {"run_id": run_id, "at": at, "cards": cards,
           "requested": len(todo), "succeeded": len(results),
           "mode": f"only:{only}" if only else "full",
           # 双写赛马数据:组合器 vs LLM 公式逐卡比对的批次汇总(权威裁决依据)
           "race": {"verdicts": race_counts, "disagree": sorted(disagree_keys),
                    "renderer_unsupported": sorted(unsup_keys)},
           "publication": pub_counts}
    (run_dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1))
    store.activate(run_id, {"at": at})
    store.prune(keep=3, protect=run_id)
    print("双写赛马: " + "  ".join(f"{k}={v}" for k, v in sorted(race_counts.items()))
          + (f"  ← 分歧卡: {sorted(disagree_keys)}" if disagree_keys else "")
          + (f"  ← 组合器未覆盖: {sorted(unsup_keys)}" if unsup_keys else ""))
    print("发布状态: " + "  ".join(f"{k}={v}" for k, v in sorted(pub_counts.items())))
    print(f"\nstore: {len(results)}/{len(todo)} 张生成,批次 {run_id} 已发布并激活 → {run_dir}")
    return 0
