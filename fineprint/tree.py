# -*- coding: utf-8 -*-
"""口径树:把 trace 的 S/F/E 三元组按公式结构重新组织成树形展示。

根 = 指标(公式骨架 + 输出维度);最外层二元运算劈成左右分支(分子/分母);
每分支带自己的展开公式(组合器产物,别名已展开成真实表名)、命名子表达式、
专属口径条件与值链路;两侧共同的条件归入公共组。

条件归属按**行集闭包**判定,不是值路径:分子经 join 关联 paid_orders 时,
paid_orders 的过滤同样约束分子行集——只看值路径会把 status='paid' 错归分母。
纯展示层:不进指纹、不进漂移快照、不改任何存储结构;任何一步失败由调用方
回退平铺视图,绝不让 trace 挂掉。
"""
from sqlglot import exp, parse_one

from fineprint.i18n import t as _t
from fineprint.lineage import dialect, table_key

# 分支标签存 (zh, en) 双语对,展示时经 _t 按当前语言取值
_OPS = {exp.Div: ("÷", ("分子", "numerator"), ("分母", "denominator")),
        exp.Mul: ("×", ("左因子", "left factor"), ("右因子", "right factor")),
        exp.Add: ("+", ("左项", "left term"), ("右项", "right term")),
        exp.Sub: ("−", ("被减项", "minuend"), ("减项", "subtrahend"))}


def _clean(sql: str) -> str:
    return sql.replace('"', "").replace("`", "")


def _cte_of(src) -> str | None:
    par = getattr(src, "expression", None)
    par = par.parent if par is not None else None
    return par.alias if isinstance(par, exp.CTE) else None


def _model_of_table(project, comp, tbl: exp.Table) -> str | None:
    try:
        rel = project.complete_rel(table_key(tbl))
    except Exception:
        return None
    return comp.rel_models.get(rel)


def _row_closure(graph, comp, seed_uids: set) -> set:
    """模型集合沿 row_set_tables(FROM/JOIN 行集伙伴)做闭包:上游模型的过滤
    经 join 同样约束当前行集。"""
    out, stack = set(), list(seed_uids)
    while stack:
        u = stack.pop()
        if u in out or u not in graph["models"]:
            continue
        out.add(u)
        for rel in graph["models"][u].get("row_set_tables") or []:
            up = comp.rel_models.get(rel)
            if up and up not in out:
                stack.append(up)
    return out


def _operand_closure(graph, comp, project, root_scope, raw_operand):
    """原始操作数 → (目标模型内 CTE 闭包名集, 上游模型 uid 闭包)。
    无限定引用(如 COUNT(*))按整个 FROM 行集处理:闭包 = 全部根来源。"""
    all_srcs = dict(root_scope.sources or {})
    keys = set(root_scope.selected_sources or {}) or set(all_srcs)
    refs = {c.table for c in raw_operand.find_all(exp.Column)
            if c.table and c.table in all_srcs}
    seeds = [all_srcs[a] for a in (refs or keys) if a in all_srcs]
    ctes, models, stack, seen = {"main"}, set(), list(seeds), set()
    while stack:
        s = stack.pop()
        if id(s) in seen:
            continue
        seen.add(id(s))
        if isinstance(s, exp.Table):
            up = _model_of_table(project, comp, s)
            if up:
                models.add(up)
            continue
        name = _cte_of(s)
        if name:
            ctes.add(name)
        for inner in (getattr(s, "sources", None) or {}).values():
            stack.append(inner)
    return ctes, _row_closure(graph, comp, models)


def _def_leaves(defs_by_name: dict, ast: exp.Expression, seen: set | None = None) -> list:
    """操作数/子表达式 AST 里的叶子列(短表名, 列名);裸名命中 def 递归其表达式。"""
    seen = seen or set()
    out = []
    for c in ast.find_all(exp.Column):
        if not c.table and c.name in defs_by_name and c.name not in seen:
            seen.add(c.name)
            try:
                sub = parse_one(defs_by_name[c.name]["expr"], read=dialect())
                out += _def_leaves(defs_by_name, sub, seen)
            except Exception:
                pass
        elif c.table:
            out.append((c.table.split(".")[-1].lower(), c.name.lower()))
    return out


def _reaches(graph, comp, uid: str, col: str, leaf_pairs: set, memo: dict) -> bool:
    """图上游走查:模型列的值链是否落到操作数的某个叶子 (表, 列)。"""
    key = (uid, col)
    if key in memo:
        return memo[key]
    memo[key] = False                     # 环保护
    m = graph["models"].get(uid) or {}
    for u in ((m.get("columns") or {}).get(col) or {}).get("upstreams") or []:
        rel, ucol = u.get("table") or "", (u.get("column") or "").lower()
        short = rel.split(".")[-1].lower()
        up = comp.rel_models.get(rel)
        if up:
            if _reaches(graph, comp, up, u.get("column") or "", leaf_pairs, memo):
                memo[key] = True
                return True
        elif (short, ucol) in leaf_pairs:
            memo[key] = True
            return True
    return memo[key]


def _unwrap(node: exp.Expression) -> exp.Expression:
    """剥透明包装(ROUND/CAST/括号):不改行集与配比结构,只挡住劈分。"""
    while isinstance(node, (exp.Paren, exp.Cast, exp.Round)):
        node = node.this
    return node


def _defining_hop(graph, comp, uid: str, col: str) -> tuple:
    """直通列下钻:app 层 `select gmv from dm_x` 的树应建在定义层(那里才有
    公式结构与 CTE 作用域)。沿"表达式=裸列引用且唯一上游是模型"逐跳下行。"""
    seen = set()
    while True:
        info = ((graph["models"].get(uid) or {}).get("columns") or {}).get(col) or {}
        try:
            ast = parse_one(info.get("expr") or "", read=dialect())
        except Exception:
            return uid, col
        node = ast.this if isinstance(ast, exp.Alias) else ast
        ups = info.get("upstreams") or []
        if not isinstance(node, exp.Column) or len(ups) != 1:
            return uid, col
        up = comp.rel_models.get(ups[0].get("table") or "")
        nxt = (up, ups[0].get("column") or "")
        if not up or nxt in seen or up not in graph["models"]:
            return uid, col
        seen.add(nxt)
        uid, col = nxt


def caliber_tree(project, graph, uid: str, column: str, t: dict) -> dict | None:
    """组合器产物 + trace 三元组 → 树结构;不可树化(top 缺失/结构不劈)返回 None。"""
    from fineprint.render import _Composer
    comp = _Composer(project, graph)
    root_uid = uid
    uid, column = _defining_hop(graph, comp, uid, column)
    c = comp.compose_target(uid, column)
    if not c.get("top"):
        return None
    top = parse_one(c["top"], read=dialect())
    scope = comp._root_scope(uid)
    raw = next((s for s in scope.expression.selects if s.alias_or_name == column), None)
    if raw is None:
        return None
    raw = raw.this if isinstance(raw, exp.Alias) else raw

    top_in, raw_in = _unwrap(top), _unwrap(raw)
    op = type(top_in)
    if op in _OPS and isinstance(raw_in, op):
        sym, lpair, rpair = _OPS[op]
        lname, rname = _t(*lpair), _t(*rpair)
        parts = [(lname, top_in.this, raw_in.this),
                 (rname, top_in.expression, raw_in.expression)]
        # 公式行直接给真实表达式(用户反馈:A/B 骨架还要对照下文,不如原式直观)
        skeleton_sql = _clean(top.sql(dialect=dialect()))
    else:
        sym, parts, skeleton_sql = None, [(_t("整体", "whole"), top_in, raw_in)], None

    defs_by_name = {d["name"]: d for d in c.get("defs") or []}
    model_name = graph["models"][uid]["name"]
    branches = []
    for label, disp_ast, raw_ast in parts:
        ctes, models = _operand_closure(graph, comp, project, scope, raw_ast)
        models.add(uid)
        leaf_pairs = set(_def_leaves(defs_by_name, disp_ast))
        used_defs = [defs_by_name[c2.name] for c2 in disp_ast.find_all(exp.Column)
                     if not c2.table and c2.name in defs_by_name]
        memo: dict = {}
        chain = []
        for e in t["expr_chain"]:
            if e["depth"] <= 0:
                continue
            if leaf_pairs:
                if _reaches(graph, comp, e["model_uid"], e["column"], leaf_pairs, memo):
                    chain.append(e)
            elif e["model_uid"] in models:
                chain.append(e)
        branches.append({
            "label": label, "formula": _clean(disp_ast.sql(dialect=dialect())),
            "defs": used_defs, "ctes": ctes, "models": models,
            "leaves": sorted({f"{a}.{b}" for a, b in leaf_pairs}),
            "chain": chain, "conds": [],
        })

    common, seen_fp = [], set()
    for cond in t["conditions"]:
        if cond.get("is_pure_key"):
            continue
        hit = []
        for b in branches:
            if cond["model"] == model_name:
                ok = cond["scope"] in b["ctes"]
            else:
                ok = any(graph["models"].get(u, {}).get("name") == cond["model"]
                         for u in b["models"])
            if ok:
                hit.append(b)
        if len(hit) == 1 and len(branches) > 1:
            hit[0]["conds"].append(cond)
        elif cond["fp"] not in seen_fp:
            seen_fp.add(cond["fp"])
            common.append(cond)

    dims = graph["models"][root_uid].get("grain") or []
    dim_models = {graph["models"][root_uid]["name"], model_name}
    dim_exprs = {}
    for s in t.get("semantics") or []:
        if s.get("type") == "stat_date_key" and s.get("model") in dim_models:
            try:                          # 展示层剥表限定:CAST(o.paid_at …) → CAST(paid_at …)
                ast = parse_one(s["sql"], read=dialect()).transform(
                    lambda n: exp.column(n.name) if isinstance(n, exp.Column) else n)
                dim_exprs.setdefault(s.get("column"), _clean(ast.sql(dialect=dialect())))
            except Exception:
                dim_exprs.setdefault(s.get("column"), _clean(s["sql"]))
    return {"target": t["target"], "op": sym, "branches": branches,
            "common": common, "dims": dims, "dim_exprs": dim_exprs,
            "skeleton": skeleton_sql,
            "defined_in": model_name if uid != root_uid else None,
            "status": c.get("status"),
            # --full 树内嵌源字段用:分支 leaves 之外的源(第三方包边界、行集 * 源)
            "sources": t.get("sources") or []}


def _cond_line(cond: dict, full: bool = False) -> str:
    where = cond["model"] if cond["scope"] in ("main", "") else f"{cond['model']}.{cond['scope']}"
    jt = f",{cond.get('join_type', '')} join {cond.get('join_table')}" if cond["kind"] == "join_on" else ""
    anchor = ""
    if full:                              # --full:出处锚点长在条件行上,替代平铺 F 块
        kind = "" if cond["kind"] == "join_on" else f"{cond['kind']} · "
        anchor = f" · {kind}{cond.get('src_path', '')} L{cond.get('line', '?')}"
    return f"{_clean(cond['sql'])}   ({where}{jt}{anchor})"


def _extra_sources(tr: dict) -> list:
    """S 集里未被任何分支 leaves 覆盖的源(第三方包边界表、COUNT(*) 的 * 源):
    --full 树必须无信息损失地替代平铺 S 块,这些单独补一行。"""
    covered = set()
    for b in tr["branches"]:
        covered.update(x.lower() for x in b["leaves"])
    out = []
    for s in tr.get("sources") or []:
        label = f"{s['table']}.{s['column']}"
        if label.lower() in covered and not s.get("package"):
            continue
        if s.get("package"):
            label += _t(f"(⟵ 第三方包 {s['package']},数据源边界)",
                        f" (⟵ third-party package {s['package']}, data-source boundary)")
        out.append(label)
    return out


def render_tree(tr: dict, full: bool = False) -> str:
    head = f"◎ {tr['target']}"
    if tr.get("defined_in"):
        head += _t(f"   (直通列,口径定义于 {tr['defined_in']})",
                   f"   (pass-through column; caliber defined in {tr['defined_in']})")
    L = [head]
    if tr["dims"]:
        dd = [f"{d} = {tr['dim_exprs'][d]}" if d in tr["dim_exprs"] else d for d in tr["dims"]]
        L.append(_t(f"│  输出维度: {' , '.join(dd)}", f"│  output dimensions: {' , '.join(dd)}"))
    if tr["op"] and tr.get("skeleton"):
        L.append(_t(f"│  公式: {tr['skeleton']}", f"│  formula: {tr['skeleton']}"))
    n = len(tr["branches"])
    for i, b in enumerate(tr["branches"]):
        L.append("│")
        L.append(f"├─ {b['label']}  {b['formula']}")
        rows = []
        for d in b["defs"]:
            g = (_t(f" 按 {','.join(d['grain'])} 聚合",
                    f" aggregated by {','.join(d['grain'])}") if d.get("grain") else "")
            j = _t("(经 join)", " (via join)") if d.get("join_context") else ""
            rows.append(_t(f"其中 {d['name']} = {_clean(d['expr'])}{g}{j}",
                           f"where {d['name']} = {_clean(d['expr'])}{g}{j}"))
        if full and b["leaves"]:
            rows.append(_t(f"源: {_t('、', ', ').join(b['leaves'])}",
                           f"sources: {', '.join(b['leaves'])}"))
        rows += [_t(f"口径: {_cond_line(c, full)}", f"caliber: {_cond_line(c, full)}")
                 for c in b["conds"]]
        if b["chain"]:
            # 按深度分层:同层多模型是平行路径,用「、」并列;层间才是流向
            by_depth: dict = {}
            for e in b["chain"]:
                by_depth.setdefault(e["depth"], set()).add(e["model"])
            hops = " → ".join(_t("、", ", ").join(sorted(by_depth[d]))
                              for d in sorted(by_depth, reverse=True))
            rows.append(_t(f"链路: {hops} → 本层", f"chain: {hops} → this layer"))
        elif b["leaves"]:
            rows.append(_t(f"链路: {' → '.join(b['leaves'])} → 本层",
                           f"chain: {' → '.join(b['leaves'])} → this layer"))
        for k, r in enumerate(rows):
            L.append(f"│  {'└─' if k == len(rows) - 1 else '├─'} {r}")
    extras = _extra_sources(tr) if full else []
    if tr["common"]:
        L.append("│")
        head = (_t("两侧共同口径", "caliber shared by both sides") if n > 1
                else _t("口径条件", "caliber conditions"))
        L.append(f"{'├─' if extras else '└─'} {head}")
        pad = "│  " if extras else "   "
        for k, c in enumerate(tr["common"]):
            L.append(f"{pad}{'└─' if k == len(tr['common']) - 1 else '├─'} {_cond_line(c, full)}")
    if extras:                            # 分支 leaves 未覆盖的源:边界表/行集 * 源
        L.append("│")
        L.append(_t("└─ 边界源(值链之外/第三方)", "└─ boundary sources (outside value chains / third-party)"))
        for k, x in enumerate(extras):
            L.append(f"   {'└─' if k == len(extras) - 1 else '├─'} {x}")
    return "\n".join(L)
