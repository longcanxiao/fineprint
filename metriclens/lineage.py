#!/usr/bin/env python3
"""血缘引擎:sqlglot 解析 dbt 编译产物 → 字段级 DAG + 过滤上下文 + 七类结构语义抽取。

- 血缘边携带表达式;每个条件携带 kind(where/having/qualify/join_on)、来源 scope、行号锚定
- join on 条件与 where 分开归档,保留「关联即过滤」语义;纯关联键单独标记
- 窗口函数识别去重/序号惯用法;CASE WHEN / COALESCE / 统计日归属逐列展开
- 条件相关性 = 值路径 scope + 行集闭包(FROM/inner join 传播,left join 阻断)
"""
import hashlib
import json
import re
from datetime import datetime

from sqlglot import exp, parse_one
from sqlglot.lineage import lineage as sg_lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.simplify import simplify

from metriclens.project import DbtProject

_DIALECT = "duckdb"


def set_dialect(d: str):
    global _DIALECT
    _DIALECT = d


def dialect() -> str:
    return _DIALECT


def table_key(e: exp.Table) -> str:
    """Table 节点 → 表引用串:SQL 写了几段就出几段('db.schema.table' /
    'schema.table' / 裸名)。图键统一三段,两段引用由建图期 complete_rel 补全。"""
    if e.catalog:
        return f"{e.catalog}.{e.db}.{e.name}"
    return f"{e.db}.{e.name}" if e.db else e.name


# ---------------- 归一化 ----------------
def normalize_condition(cond: exp.Expression) -> str:
    """AST 级归一化:常量折叠、比较方向标准化、去限定名,输出规范文本。"""
    c = cond.copy()
    try:
        c = simplify(c)
    except Exception:
        pass
    FLIP = {exp.GT: exp.LT, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.LTE: exp.GTE}
    for node in list(c.walk()):
        t = type(node)
        if t in FLIP and isinstance(node.this, exp.Literal) and not isinstance(node.expression, exp.Literal):
            node.replace(FLIP[t](this=node.expression, expression=node.this))
    for col in list(c.find_all(exp.Column)):
        col.replace(exp.column(col.name))
    s = c.sql(dialect=_DIALECT).lower()
    return re.sub(r"\s+", " ", s).strip()


def fingerprint(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:12]


# ---------------- 行号锚定 ----------------
def anchor_line(cond_sql: str, file_text: str) -> int | None:
    def norm(s):
        return re.sub(r"\s+", " ", s.replace('"', "").replace("_", "").lower()).strip()

    target = norm(cond_sql)
    lines = file_text.splitlines()
    for i, ln in enumerate(lines, 1):
        if target and target in norm(ln):
            return i
    toks = [t.replace("_", "") for t in re.findall(r"[a-z_]{3,}|\d+", cond_sql.lower()) if t not in
            ("and", "when", "then", "else", "case", "end", "select", "from", "where", "the", "cast", "as", "not")]
    best = sorted(set(toks), key=len, reverse=True)[:3]
    for k in (3, 2, 1):
        for i, ln in enumerate(lines, 1):
            n = norm(ln)
            if best[:k] and all(b in n for b in best[:k]):
                return i
    return None


# ---------------- 条件与语义抽取 ----------------
def split_and(e: exp.Expression):
    if isinstance(e, exp.And):
        yield from split_and(e.this)
        yield from split_and(e.expression)
    else:
        yield e


def _scope_base(anc: exp.Expression) -> str:
    return anc.alias if isinstance(anc, exp.CTE) else (anc.alias_or_name or "_subq")


def scope_ids(ast: exp.Expression) -> dict:
    """CTE/内联子查询节点 → 唯一 scope 名。别名可被合法复用(并列/嵌套同名子查询),
    以前序出现次序加 @n 后缀消歧——否则重名 scope 合并,left join 子查询的内部
    条件会蹭上同名 FROM scope 的行集资格。首个保持裸名,与列级血缘的 scope 名兼容。"""
    ids: dict = {}
    seen: dict = {}
    for node in ast.walk():
        if isinstance(node, (exp.CTE, exp.Subquery)):
            base = _scope_base(node)
            n = seen.get(base, 0) + 1
            seen[base] = n
            ids[id(node)] = base if n == 1 else f"{base}@{n}"
    return ids


def scope_name(node: exp.Expression, ids: dict | None = None) -> str:
    """所属作用域:最近的 CTE/内联子查询的唯一 scope 名;顶层为 main。
    内联子查询必须有独立 scope——否则 left join (select … where …) 的内部条件
    会被误标 main 并进入行集闭包(实际只影响补列)。"""
    anc = node.find_ancestor(exp.CTE, exp.Subquery)
    if anc is None:
        return "main"
    if ids is not None:
        return ids.get(id(anc)) or _scope_base(anc)
    return _scope_base(anc)


def from_arg(sel: exp.Select):
    """sqlglot 30 起 Select 的 from 参数改名 from_;两代 key 兼容读取。"""
    return sel.args.get("from") or sel.args.get("from_")


def _scope_child(t, cte_names: set, ids: dict) -> str | None:
    """FROM/JOIN 挂接对象 → 子 scope 名(CTE 名或内联子查询唯一名);真实表返回 None。"""
    if isinstance(t, exp.Table) and t.name in cte_names:
        return t.name
    if isinstance(t, exp.Subquery):
        return ids.get(id(t)) or _scope_base(t)
    return None


def _scope_edges(ast: exp.Expression, ids: dict) -> dict:
    """scope → [(子 scope, 挂接方式)]:from / inner / left / right / full。"""
    cte_names = {c.alias for c in ast.find_all(exp.CTE)}
    edges: dict = {}
    for sel in ast.find_all(exp.Select):
        outs = edges.setdefault(scope_name(sel, ids), [])
        f = from_arg(sel)
        if f is not None:
            child = _scope_child(f.this, cte_names, ids)
            if child:
                outs.append((child, "from"))
        for j in sel.args.get("joins") or []:
            child = _scope_child(j.this, cte_names, ids)
            if child:
                outs.append((child, (j.side or "inner").lower()))
    return edges


def _closure(edges: dict, kinds: tuple | None, root: str = "main") -> set:
    """root 出发沿边可达的 scope 集合;kinds=None 表示全部挂接方式。"""
    closure, stack = {root}, [root]
    while stack:
        cur = stack.pop()
        for child, kind in edges.get(cur, []):
            if (kinds is None or kind in kinds) and child not in closure:
                closure.add(child)
                stack.append(child)
    return closure


def row_scope_closure(raw_ast: exp.Expression, ids: dict | None = None) -> set:
    """条件行集闭包:main 沿 FROM 与 inner join 可达的 scope——其条件约束整个行集;
    left join 挂接的不在内:内部条件只影响补列的值。"""
    ids = ids if ids is not None else scope_ids(raw_ast)
    return _closure(_scope_edges(raw_ast, ids), ("from", "inner"))


def row_set_tables(ast: exp.Expression) -> list:
    """行数依赖闭包内引用的真实表(排除 CTE 名):COUNT(*) 等无列引用的输出列的
    行数由 FROM 与全部 join 表共同决定——inner 收缩、left 一对多膨胀都改变行数,
    因此这里沿所有 join 方向收表,比条件行集闭包(排除 left)更宽。"""
    ids = scope_ids(ast)
    cte_names = {c.alias for c in ast.find_all(exp.CTE)}
    closure = _closure(_scope_edges(ast, ids), None)
    tables = []

    def add(t):
        if isinstance(t, exp.Table) and t.name not in cte_names:
            tk = table_key(t)
            if tk not in tables:
                tables.append(tk)

    for sel in ast.find_all(exp.Select):
        if scope_name(sel, ids) not in closure:
            continue
        f = from_arg(sel)
        if f is not None:
            add(f.this)
        for j in sel.args.get("joins") or []:
            add(j.this)
    return tables


def _partner_keys(j) -> list:
    """join 伙伴侧的等值键列名(lower):ON 等值两侧取属伙伴别名的列 + USING 列。"""
    alias = j.this.alias_or_name if isinstance(j.this, (exp.Table, exp.Subquery)) else None
    keys = set()
    on = j.args.get("on")
    for c in split_and(on) if on is not None else ():
        if isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) \
                and isinstance(c.expression, exp.Column):
            for col in (c.this, c.expression):
                if col.table == alias:
                    keys.add(col.name.lower())
    for u in j.args.get("using") or []:
        keys.add(str(u.name).lower())
    return sorted(keys)


def _node_unique_keys(node) -> list:
    """CTE/子查询的输出唯一键:分组键或窗口去重键,二者皆为确定性基数证据。"""
    sel = node.this.find(exp.Select) if isinstance(node, (exp.CTE, exp.Subquery)) else None
    if sel is None:
        return []
    return _select_grain(sel) or _select_dedup_keys(sel)


def output_unique_on(ast: exp.Expression) -> list:
    """输出唯一键(窗口去重证据):沿 main → FROM 主链找去重 SELECT 的 partition 键,
    供治理把"join 到本模型且键覆盖 unique_on"判为 N:1 安全。
    先遇 group-by 由 grain 承担唯一性(返回 []);途中任一 join 若不能就地证明
    N:1(伙伴唯一键被 join 键覆盖),行可能被复制,唯一性主张作废。"""
    ids = scope_ids(ast)
    cte_def = {c.alias: c for c in ast.find_all(exp.CTE)}
    sel_by_scope: dict = {}
    for sel in ast.find_all(exp.Select):
        sel_by_scope.setdefault(scope_name(sel, ids), sel)
    cur, seen = "main", set()
    while cur in sel_by_scope and cur not in seen:
        seen.add(cur)
        sel = sel_by_scope[cur]
        if sel.args.get("group"):
            return []
        for j in sel.args.get("joins") or []:
            t = j.this
            node = cte_def.get(t.name) if isinstance(t, exp.Table) and t.name in cte_def \
                else (t if isinstance(t, exp.Subquery) else None)
            u = _node_unique_keys(node) if node is not None else []
            if not (u and set(u) <= set(_partner_keys(j))):
                return []
        keys = _select_dedup_keys(sel)
        if keys:
            return keys
        f = from_arg(sel)
        if f is not None and isinstance(f.this, exp.Table):
            cur = f.this.name
        elif f is not None and isinstance(f.this, exp.Subquery):
            cur = ids.get(id(f.this)) or _scope_base(f.this)
        else:
            break
    return []


def row_risk_joins(ast: exp.Expression) -> list:
    """行基数风险 join 清单:全 join 方向闭包内的 join 伙伴 + 伙伴侧等值键。

    值来源与行集依赖是两类血缘:SUM(a.amount) 在 a LEFT JOIN b 上会被 b 的一对多
    放大,值链却只见 a.amount。治理判重必须比较行结构——但"join 即风险"过宽:
    join 键覆盖伙伴的分组键(grain)时,伙伴对键唯一,N:1 可证安全。
    本函数在模型内就地消化 CTE/子查询伙伴(局部 grain 可判);真实表伙伴的
    grain 归上游模型,键随条目带出,由治理 scan 结合图上 grain 终判。
    返回 [{rel, keys}]:keys 为伙伴侧等值列名(lower);无键(非等值/USING 之外)= []。"""
    ids = scope_ids(ast)
    cte_names = {c.alias for c in ast.find_all(exp.CTE)}
    cte_def = {c.alias: c for c in ast.find_all(exp.CTE)}
    edges = _scope_edges(ast, ids)
    closure = _closure(edges, None)
    out = []

    def branch_tables(scope: str) -> list:
        """风险 CTE/子查询伙伴的内部真实表(该分支闭包内)。"""
        tabs = []
        reach = _closure(edges, None, root=scope)
        for sel in ast.find_all(exp.Select):
            if scope_name(sel, ids) not in reach:
                continue
            for t in [f.this for f in [from_arg(sel)] if f is not None] \
                    + [j.this for j in sel.args.get("joins") or []]:
                if isinstance(t, exp.Table) and t.name not in cte_names:
                    tk = table_key(t)
                    if tk not in tabs:
                        tabs.append(tk)
        return tabs

    for sel in ast.find_all(exp.Select):
        if scope_name(sel, ids) not in closure:
            continue
        for j in sel.args.get("joins") or []:
            keys = _partner_keys(j)
            t = j.this
            if isinstance(t, exp.Table) and t.name not in cte_names:
                out.append({"rel": table_key(t), "keys": keys})
                continue
            # CTE / 内联子查询伙伴:局部唯一键可判——join 键覆盖即 N:1 安全
            node = cte_def.get(t.name) if isinstance(t, exp.Table) else (
                t if isinstance(t, exp.Subquery) else None)
            if node is None:
                continue
            u = _node_unique_keys(node)
            if u and set(u) <= set(keys):
                continue
            child = t.name if isinstance(t, exp.Table) else (ids.get(id(t)) or _scope_base(t))
            for tk in branch_tables(child):
                out.append({"rel": tk, "keys": []})   # 分支内部表:键不可传导,保守视为风险
    return out


def extract_conditions(raw_ast: exp.Expression, file_text: str, model: str):
    """七类口径藏身结构的语义化抽取(基于未 qualify 的原始 AST,便于行号锚定)。"""
    conds, semantics = [], []
    ids = scope_ids(raw_ast)
    row_scopes = row_scope_closure(raw_ast, ids)

    def add_cond(kind, e, extra=None, row_level=None):
        sql = e.sql(dialect=_DIALECT)
        norm = normalize_condition(e)
        sc = scope_name(e, ids)
        conds.append({
            "model": model, "scope": sc, "kind": kind,
            "row_level": (sc in row_scopes) if row_level is None else row_level,
            "sql": sql, "norm": norm, "fp": fingerprint(norm),
            "line": anchor_line(sql, file_text), **(extra or {}),
        })

    for sel in raw_ast.find_all(exp.Select):
        w = sel.args.get("where")
        if w:
            for c in split_and(w.this):
                add_cond("where", c)
        h = sel.args.get("having")
        if h:
            for c in split_and(h.this):
                add_cond("having", c)
        ql = sel.args.get("qualify")
        if ql:
            for c in split_and(ql.this):
                add_cond("qualify", c)
        for j in sel.args.get("joins") or []:
            side = (j.side or "") + (" " + j.kind if j.kind else "") or "inner"
            # 非 inner join 的 ON 条件不过滤主行集(left 只决定补列是否匹配),
            # 不得标 row_level——否则右表筛选会被误并入 COUNT(*) 类指标的行集口径
            j_row = None if (j.side or "inner").lower() == "inner" else False
            jt = j.this
            jtable = jt.name if isinstance(jt, (exp.Table, exp.Subquery)) else jt.sql(dialect=_DIALECT)[:40]
            on = j.args.get("on")
            if on:
                for c in split_and(on):
                    is_key = (isinstance(c, exp.EQ) and isinstance(c.this, exp.Column)
                              and isinstance(c.expression, exp.Column))
                    add_cond("join_on", c, {"join_type": side.strip(), "join_table": jtable,
                                            "is_pure_key": bool(is_key)}, row_level=j_row)
            using = j.args.get("using")
            if using:
                add_cond("join_on", exp.column(using[0].name), {"join_type": side.strip(),
                         "join_table": jtable, "is_pure_key": True}, row_level=j_row)

    for w in raw_ast.find_all(exp.Window):
        func = w.this.sql(dialect=_DIALECT).lower()
        part = [p.sql(dialect=_DIALECT) for p in (w.args.get("partition_by") or [])]
        order = w.args.get("order")
        order_s = order.sql(dialect=_DIALECT).replace("ORDER BY ", "") if order else ""
        sel = w.find_ancestor(exp.Select)
        has_eq1 = False
        if sel is not None:
            ql = sel.args.get("qualify")
            if ql and "= 1" in re.sub(r"\s+", " ", ql.sql(dialect=_DIALECT)):
                has_eq1 = True
        idiom = ("dedup" if func.startswith("row_number") and has_eq1
                 else "sequence" if func.startswith("row_number")
                 else "window_agg")
        wsql = w.sql(dialect=_DIALECT)
        walias = w.find_ancestor(exp.Alias)
        semantics.append({
            "model": model, "scope": scope_name(w, ids), "type": "window", "idiom": idiom,
            "column": walias.alias if walias is not None else None,
            "func": func, "partition_by": part, "order_by": order_s,
            "sql": wsql, "line": anchor_line(wsql, file_text),
        })

    for sel in raw_ast.find_all(exp.Select):
        for proj in sel.expressions:
            colname = proj.alias_or_name
            for node, tname in ((proj.find(exp.Case), "case_when"), (proj.find(exp.Coalesce), "coalesce")):
                if node is not None:
                    nsql = node.sql(dialect=_DIALECT)
                    semantics.append({
                        "model": model, "scope": scope_name(proj, ids), "type": tname,
                        "column": colname, "sql": nsql[:200], "line": anchor_line(nsql, file_text),
                    })
    for sel in raw_ast.find_all(exp.Select):
        g = sel.args.get("group")
        if not g:
            continue
        proj_by_pos = {str(i + 1): p for i, p in enumerate(sel.expressions)}
        proj_by_name = {p.alias_or_name: p for p in sel.expressions}
        for k in g.expressions:
            ks = k.sql(dialect=_DIALECT)
            p = proj_by_pos.get(ks) or proj_by_name.get(ks)
            expr_sql = (p.this if isinstance(p, exp.Alias) else p).sql(dialect=_DIALECT) if p is not None else ks
            name = p.alias_or_name if p is not None else ks
            if expr_sql.strip().strip('"') == str(name):
                continue
            if re.search(r"date|dt|day", str(name).lower()) or "date" in expr_sql.lower():
                semantics.append({
                    "model": model, "scope": scope_name(g, ids), "type": "stat_date_key",
                    "column": name, "sql": expr_sql[:160], "line": anchor_line(expr_sql, file_text),
                })
    # 行数型聚合跨 join = 口径模糊的 SQL 质量问题:count(*) 数的是"匹配对",
    # 它数的到底是哪张表寄生在 join 键唯一性与数据覆盖性上——SQL 未自证,
    # 底层数据变化口径即静默漂移。列举行集全部参与表与关联键,供治理报告立项。
    edges = _scope_edges(raw_ast, ids)
    cte_names = {c.alias for c in raw_ast.find_all(exp.CTE)}
    for sel in raw_ast.find_all(exp.Select):
        for proj in sel.expressions:
            agg = next((f for f in proj.find_all(exp.AggFunc)
                        if agg_one(f) == "rowcount" and f.find_ancestor(exp.Window) is None), None)
            if agg is None:
                continue
            reach = _closure(edges, None, root=scope_name(sel, ids))
            tabs, keys, njoin = [], [], 0
            for s2 in raw_ast.find_all(exp.Select):
                if scope_name(s2, ids) not in reach:
                    continue
                f2 = from_arg(s2)
                if f2 is not None and isinstance(f2.this, exp.Table) and f2.this.name not in cte_names:
                    tk = table_key(f2.this)
                    if tk not in tabs:
                        tabs.append(tk)
                for j in s2.args.get("joins") or []:
                    njoin += 1
                    if isinstance(j.this, exp.Table) and j.this.name not in cte_names:
                        tk = table_key(j.this)
                        if tk not in tabs:
                            tabs.append(tk)
                    on = j.args.get("on")
                    if on is not None:
                        keys.append(on.sql(dialect=_DIALECT)[:80])
            if njoin == 0:
                continue          # 单表行集的 count(*) 计数对象无歧义,不立项
            psql = proj.sql(dialect=_DIALECT)
            semantics.append({
                "model": model, "scope": scope_name(sel, ids), "type": "join_count",
                "column": proj.alias_or_name, "tables": tabs, "join_keys": keys[:6],
                "sql": psql[:120], "line": anchor_line(psql, file_text),
            })
    # 别名合法复用(同名 scope 现于多处,ids 里带 @n 消歧)时,列级血缘的 scope 名是
    # 裸别名,无法区分条件到底属于哪个同名 scope——这类条件/语义点打上 scope_dup,
    # trace 不做值路径归因,单独暴露为"归因不明"(见 trace.scope_ambiguous)
    dup_bases = {v.split("@")[0] for v in ids.values() if "@" in v}
    if dup_bases:
        for c in conds:
            if c["scope"].split("@")[0] in dup_bases:
                c["scope_dup"] = True
        for s in semantics:
            if str(s.get("scope", "")).split("@")[0] in dup_bases:
                s["scope_dup"] = True
    return conds, semantics


def _select_grain(sel: exp.Select) -> list:
    """单个 SELECT 的分组键(序号/源表达式归位到投影别名);无 group-by 返回 []。
    group-by 的输出对分组键唯一——这是 SQL 自带的确定性基数证据。"""
    g = sel.args.get("group")
    if not g:
        return []
    proj_by_pos = {str(i + 1): p for i, p in enumerate(sel.expressions)}
    proj_by_name = {p.alias_or_name: p for p in sel.expressions}
    # qualify 会把 group by 1 归一成源列/表达式:按投影内层表达式再对一轮
    proj_by_expr = {(p.this if isinstance(p, exp.Alias) else p).sql(dialect=_DIALECT): p
                    for p in sel.expressions}
    keys = set()
    for k in g.expressions:
        ks = k.name if isinstance(k, exp.Column) else k.sql(dialect=_DIALECT)
        p = (proj_by_pos.get(ks) or proj_by_name.get(ks)
             or proj_by_expr.get(k.sql(dialect=_DIALECT)))
        name = p.alias_or_name if p is not None else ks
        keys.add(str(name).strip('"').lower())
    return sorted(keys)


def _select_dedup_keys(sel: exp.Select) -> list:
    """单个 SELECT 的窗口去重键:qualify row_number() over (partition by K …) = 1
    → 输出对 K 唯一。与 group-by 同级的确定性基数证据(T9 类去重惯用法)。"""
    ql = sel.args.get("qualify")
    if not ql or "= 1" not in re.sub(r"\s+", " ", ql.sql(dialect=_DIALECT)):
        return []
    for w in sel.find_all(exp.Window):
        if w.find_ancestor(exp.Select) is not sel:
            continue
        if not w.this.sql(dialect=_DIALECT).lower().startswith("row_number"):
            continue
        keys = {(p.name if isinstance(p, exp.Column) else p.sql(dialect=_DIALECT)).lower()
                for p in (w.args.get("partition_by") or [])}
        if keys:
            return sorted(k.strip('"') for k in keys)
    return []


def output_grain(ast: exp.Expression) -> list:
    """输出行粒度:沿 main → FROM 主链找第一个带 group-by 的 SELECT,解析其分组键
    (序号/表达式归位到投影别名)。顶层 join 拼列不改变行粒度,粒度由主链聚合层决定;
    全链无分组(明细/纯直通)返回 []。治理指纹用它区分"同指标家族的不同粒度物化"。"""
    ids = scope_ids(ast)
    sel_by_scope: dict = {}
    for sel in ast.find_all(exp.Select):   # 前序遍历:每个 scope 首个 select 即其顶层
        sel_by_scope.setdefault(scope_name(sel, ids), sel)
    cur, seen = "main", set()
    while cur in sel_by_scope and cur not in seen:
        seen.add(cur)
        sel = sel_by_scope[cur]
        keys = _select_grain(sel)
        if keys:
            return keys
        f = from_arg(sel)
        if f is not None and isinstance(f.this, exp.Table):
            cur = f.this.name              # 继续沿 FROM 进入 CTE;真实表则自然终止
        elif f is not None and isinstance(f.this, exp.Subquery):
            cur = ids.get(id(f.this)) or _scope_base(f.this)   # 内联聚合子查询同样在主链上
        else:
            break
    return []


def agg_one(f: exp.AggFunc) -> str:
    """单个聚合的规范签名;等价类归一化:
    行数类   COUNT(*) ≡ COUNT(1) ≡ COUNT(常量) ≡ SUM(1);
    条件计数 SUM(CASE WHEN P THEN 1 ELSE 0/NULL END) ≡ COUNT(CASE WHEN P THEN 1 END)
             ≡ COUNT(x)(x 非空计数即 P = x IS NOT NULL 的条件计数)→ 归为 count。
    等价写法无法穷举——签名不同的判定只用于确定性"不同义"直判,新等价形按发现补录,
    误判方向是"该判等价的被判不同义"(保守,不会误判重复)。"""
    arg = f.this
    if isinstance(f, exp.Count) and (arg is None or isinstance(arg, (exp.Star, exp.Literal))):
        return "rowcount"
    if isinstance(f, exp.Sum) and isinstance(arg, exp.Literal) and arg.name == "1":
        return "rowcount"
    if isinstance(f, exp.Sum) and isinstance(arg, exp.Case):
        def _01(x):
            return (x is None or isinstance(x, exp.Null)
                    or (isinstance(x, exp.Literal) and x.name in ("0", "1")))
        ifs = arg.args.get("ifs") or []
        if ifs and all(_01(i.args.get("true")) for i in ifs) and _01(arg.args.get("default")):
            return "count"
    return type(f).__name__.lower() + (":distinct" if f.find(exp.Distinct) else "")


def model_agg_fns(raw_ast: exp.Expression) -> list:
    """模型 SQL 中出现过的全部聚合签名(含 CTE 内投影——列级 expr 只存顶层直通,
    min(case …) 藏在 CTE 里时这是唯一的确定性记录)。"""
    return sorted({agg_one(f) for f in raw_ast.find_all(exp.AggFunc)})


# ---------------- 图构建 ----------------
def build_graph(project: DbtProject) -> dict:
    from metriclens.project import rel3
    set_dialect(project.dialect)
    graph = {
        "meta": {
            "dialect": project.dialect, "adapter_type": project.adapter_type,
            "project_dir": str(project.project_dir),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            # v3:models 主键 = dbt unique_id(逻辑身份),relations 反查键 = 物理三段名
            "metriclens_graph_version": 3,
        },
        "relations": {"models": dict(project.model_by_relation),
                      "sources": dict(project.source_by_relation),
                      # 第三方包模型 = 数据源边界:血缘在此截止,只记名字与归属包
                      "external": {rel: {"name": e["name"], "package": e["package"]}
                                   for rel, e in project.external_models.items()}},
        "models": {},
    }
    # 短名重名计数:conditions/semantics 的 model 字段是展示标签(漂移快照按它比对),
    # 与 trace.display_name 同规则——唯一用短名,重名用 pkg:name
    name_count: dict = {}
    for m in project.models.values():
        name_count[m["name"]] = name_count.get(m["name"], 0) + 1
    for uid, m in project.models.items():
        disp = m["name"] if name_count[m["name"]] == 1 else f'{m["package"]}:{m["name"]}'
        raw = parse_one(m["sql"], read=_DIALECT)
        qast = qualify(raw.copy(), schema=project.schema, dialect=_DIALECT)
        conds, semantics = extract_conditions(raw, m["sql"], disp)
        # qualified 表名至少带 schema;统一补全为三段物理键,可反查模型/源表
        rowset = [project.complete_rel(tk) for tk in row_set_tables(qast)]
        risk_joins = [{"rel": project.complete_rel(e["rel"]), "keys": e["keys"]}
                      for e in row_risk_joins(qast)]
        agg_fns = model_agg_fns(raw)
        out_cols = [p.alias_or_name for p in qast.expressions]
        col_edges = {}
        for col in out_cols:
            try:
                node = sg_lineage(col, qast, schema=project.schema, dialect=_DIALECT)
            except Exception as e:
                col_edges[col] = {"error": str(e)[:120], "upstreams": [], "expr": None}
                continue
            ups, seen = [], set()
            scopes = {"main"}
            stack = [node]
            while stack:
                n = stack.pop()
                ref = getattr(n, "reference_node_name", "")
                if ref:
                    scopes.add(ref)
                if n.downstream:
                    stack.extend(n.downstream)
                    continue
                t = n.expression.find(exp.Table) if not isinstance(n.expression, exp.Table) else n.expression
                if t is None:
                    continue
                key = (project.complete_rel(table_key(t)), str(n.name).split(".")[-1].strip('"'))
                if key not in seen:
                    seen.add(key)
                    ups.append({"table": key[0], "column": key[1]})
            proj_e = next((p for p in qast.expressions if p.alias_or_name == col), None)
            expr_sql = (proj_e.this if isinstance(proj_e, exp.Alias) else proj_e).sql(dialect=_DIALECT) if proj_e is not None else None
            # COUNT(*) 等无列引用的投影:列级血缘为空,但行数由行集表决定 → 表级上游兜底
            if not ups and proj_e is not None and proj_e.find(exp.Column) is None:
                ups = [{"table": tk, "column": "*"} for tk in rowset]
            col_edges[col] = {"upstreams": ups, "expr": expr_sql, "scopes": sorted(scopes)}
        graph["models"][uid] = {
            "name": m["name"], "package": m["package"],
            "layer": m["layer"], "table": rel3(m["database"], m["schema"], m["alias"]),
            "compiled_path": m["compiled_path"], "src_path": m["src_path"],
            "row_set_tables": rowset, "row_risk_joins": risk_joins,
            "agg_fns": agg_fns, "grain": output_grain(raw),
            "unique_on": output_unique_on(raw),
            "columns": col_edges, "conditions": conds, "semantics": semantics,
        }
    return graph


def save_graph(project: DbtProject, graph: dict) -> None:
    out = project.graph_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(graph, ensure_ascii=False, indent=1))
    tmp.replace(out)


def build_and_save(project: DbtProject) -> dict:
    graph = build_graph(project)
    save_graph(project, graph)
    return graph
