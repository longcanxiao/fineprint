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
    """qualified Table 节点 → 'schema.table'(去 database 层)。"""
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


def scope_name(node: exp.Expression) -> str:
    """所属作用域:最近的 CTE 别名或内联子查询别名;顶层为 main。
    内联子查询必须有独立 scope——否则 left join (select … where …) 的内部条件
    会被误标 main 并进入行集闭包(实际只影响补列)。"""
    anc = node.find_ancestor(exp.CTE, exp.Subquery)
    if anc is None:
        return "main"
    if isinstance(anc, exp.CTE):
        return anc.alias
    return anc.alias_or_name or "_subq"


def from_arg(sel: exp.Select):
    """sqlglot 30 起 Select 的 from 参数改名 from_;两代 key 兼容读取。"""
    return sel.args.get("from") or sel.args.get("from_")


def _scope_child(t, cte_names: set) -> str | None:
    """FROM/JOIN 挂接对象 → 子 scope 名(CTE 名或内联子查询别名);真实表返回 None。"""
    if isinstance(t, exp.Table) and t.name in cte_names:
        return t.name
    if isinstance(t, exp.Subquery):
        return t.alias_or_name or "_subq"
    return None


def row_scope_closure(raw_ast: exp.Expression) -> set:
    """main 出发,沿 FROM 与 inner join 可达的 CTE/内联子查询 scope 集合——
    其条件约束整个行集;left join 挂接的不在闭包内:内部条件只影响补列的值。"""
    cte_names = {c.alias for c in raw_ast.find_all(exp.CTE)}
    edges = {}
    for sel in raw_ast.find_all(exp.Select):
        sc = scope_name(sel)
        outs = edges.setdefault(sc, [])
        f = from_arg(sel)
        if f is not None:
            child = _scope_child(f.this, cte_names)
            if child:
                outs.append((child, "from"))
        for j in sel.args.get("joins") or []:
            child = _scope_child(j.this, cte_names)
            if child:
                outs.append((child, (j.side or "inner").lower()))
    closure, stack = {"main"}, ["main"]
    while stack:
        cur = stack.pop()
        for child, kind in edges.get(cur, []):
            if kind in ("from", "inner") and child not in closure:
                closure.add(child)
                stack.append(child)
    return closure


def row_set_tables(ast: exp.Expression) -> list:
    """行集闭包内引用的真实表(排除 CTE 名):COUNT(*) 等无列引用的输出列
    没有列级血缘可走,其行数仍由这些表(经行集条件过滤)决定,以表级上游兜底。"""
    cte_names = {c.alias for c in ast.find_all(exp.CTE)}
    closure = row_scope_closure(ast)
    tables = []

    def add(t):
        if isinstance(t, exp.Table) and t.name not in cte_names:
            tk = table_key(t)
            if tk not in tables:
                tables.append(tk)

    for sel in ast.find_all(exp.Select):
        if scope_name(sel) not in closure:
            continue
        f = from_arg(sel)
        if f is not None:
            add(f.this)
        for j in sel.args.get("joins") or []:
            if (j.side or "inner").lower() == "inner":
                add(j.this)
    return tables


def extract_conditions(raw_ast: exp.Expression, file_text: str, model: str):
    """七类口径藏身结构的语义化抽取(基于未 qualify 的原始 AST,便于行号锚定)。"""
    conds, semantics = [], []
    row_scopes = row_scope_closure(raw_ast)

    def add_cond(kind, e, extra=None):
        sql = e.sql(dialect=_DIALECT)
        norm = normalize_condition(e)
        sc = scope_name(e)
        conds.append({
            "model": model, "scope": sc, "kind": kind, "row_level": sc in row_scopes,
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
            jt = j.this
            jtable = jt.name if isinstance(jt, (exp.Table, exp.Subquery)) else jt.sql(dialect=_DIALECT)[:40]
            on = j.args.get("on")
            if on:
                for c in split_and(on):
                    is_key = (isinstance(c, exp.EQ) and isinstance(c.this, exp.Column)
                              and isinstance(c.expression, exp.Column))
                    add_cond("join_on", c, {"join_type": side.strip(), "join_table": jtable,
                                            "is_pure_key": bool(is_key)})
            using = j.args.get("using")
            if using:
                add_cond("join_on", exp.column(using[0].name), {"join_type": side.strip(),
                         "join_table": jtable, "is_pure_key": True})

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
            "model": model, "scope": scope_name(w), "type": "window", "idiom": idiom,
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
                        "model": model, "scope": scope_name(proj), "type": tname,
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
                    "model": model, "scope": scope_name(g), "type": "stat_date_key",
                    "column": name, "sql": expr_sql[:160], "line": anchor_line(expr_sql, file_text),
                })
    return conds, semantics


def output_grain(ast: exp.Expression) -> list:
    """输出行粒度:沿 main → FROM 主链找第一个带 group-by 的 SELECT,解析其分组键
    (序号/表达式归位到投影别名)。顶层 join 拼列不改变行粒度,粒度由主链聚合层决定;
    全链无分组(明细/纯直通)返回 []。治理指纹用它区分"同指标家族的不同粒度物化"。"""
    sel_by_scope: dict = {}
    for sel in ast.find_all(exp.Select):   # 前序遍历:每个 scope 首个 select 即其顶层
        sel_by_scope.setdefault(scope_name(sel), sel)
    cur, seen = "main", set()
    while cur in sel_by_scope and cur not in seen:
        seen.add(cur)
        sel = sel_by_scope[cur]
        g = sel.args.get("group")
        if g:
            proj_by_pos = {str(i + 1): p for i, p in enumerate(sel.expressions)}
            proj_by_name = {p.alias_or_name: p for p in sel.expressions}
            keys = set()
            for k in g.expressions:
                ks = k.name if isinstance(k, exp.Column) else k.sql(dialect=_DIALECT)
                p = proj_by_pos.get(ks) or proj_by_name.get(ks)
                name = p.alias_or_name if p is not None else ks
                keys.add(str(name).strip('"').lower())
            return sorted(keys)
        f = from_arg(sel)
        if f is not None and isinstance(f.this, exp.Table):
            cur = f.this.name              # 继续沿 FROM 进入 CTE;真实表则自然终止
        elif f is not None and isinstance(f.this, exp.Subquery):
            cur = f.this.alias_or_name or "_subq"   # 内联聚合子查询同样在主链上
        else:
            break
    return []


# ---------------- 图构建 ----------------
def build_graph(project: DbtProject) -> dict:
    set_dialect(project.dialect)
    graph = {
        "meta": {
            "dialect": project.dialect, "adapter_type": project.adapter_type,
            "project_dir": str(project.project_dir),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "metriclens_graph_version": 2,
        },
        "relations": {"models": dict(project.model_by_relation),
                      "sources": dict(project.source_by_relation)},
        "models": {},
    }
    for name, m in project.models.items():
        raw = parse_one(m["sql"], read=_DIALECT)
        qast = qualify(raw.copy(), schema=project.schema, dialect=_DIALECT)
        conds, semantics = extract_conditions(raw, m["sql"], name)
        rowset = row_set_tables(qast)      # qualified 表名带 schema,可反查模型/源表
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
                key = (table_key(t), str(n.name).split(".")[-1].strip('"'))
                if key not in seen:
                    seen.add(key)
                    ups.append({"table": key[0], "column": key[1]})
            proj_e = next((p for p in qast.expressions if p.alias_or_name == col), None)
            expr_sql = (proj_e.this if isinstance(proj_e, exp.Alias) else proj_e).sql(dialect=_DIALECT) if proj_e is not None else None
            # COUNT(*) 等无列引用的投影:列级血缘为空,但行数由行集表决定 → 表级上游兜底
            if not ups and proj_e is not None and proj_e.find(exp.Column) is None:
                ups = [{"table": tk, "column": "*"} for tk in rowset]
            col_edges[col] = {"upstreams": ups, "expr": expr_sql, "scopes": sorted(scopes)}
        graph["models"][name] = {
            "layer": m["layer"], "table": f'{m["schema"]}.{m["alias"]}',
            "compiled_path": m["compiled_path"], "src_path": m["src_path"],
            "row_set_tables": rowset, "grain": output_grain(raw),
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
