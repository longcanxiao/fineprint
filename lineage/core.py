#!/usr/bin/env python3
"""MetricLens 血缘引擎核心:sqlglot 解析 dbt 编译产物 → 字段级 DAG + 过滤上下文 + 七类结构语义抽取。

设计原则(产品方案 6.1):
- 血缘边携带表达式;每个条件携带 kind(where/having/qualify/join_on)与来源 scope、模型文件、行号锚定
- join on 条件与 where 分开归档,保留「关联即过滤」语义
- 窗口函数识别去重/序号两种惯用法;CASE WHEN / COALESCE / 单位换算从表达式树逐列展开
- group by 的日期键单独归档为统计日归属
"""
import hashlib
import json
import os
import re
from pathlib import Path

import duckdb
from sqlglot import exp, parse_one
from sqlglot.lineage import lineage as sg_lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.simplify import simplify

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("METRICLENS_DB") or ROOT / "warehouse" / "metriclens.duckdb")
COMPILED = ROOT / "warehouse" / "dbt_project" / "target" / "compiled" / "metriclens_dwh" / "models"
MODELS_SRC = ROOT / "warehouse" / "dbt_project" / "models"
DIALECT = "duckdb"


# ---------------- schema ----------------
def load_schema():
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute("""select table_catalog, table_schema, table_name, column_name, data_type
        from information_schema.columns
        where table_schema in ('ods','dwd','dwm','dm','app') and table_name != 'sim_meta'
        order by table_name, ordinal_position""").fetchall()
    con.close()
    schema = {}
    for cat, sch, tbl, col, typ in rows:
        schema.setdefault(cat, {}).setdefault(sch, {}).setdefault(tbl, {})[col] = typ
    return schema


def discover_models():
    """compiled SQL 文件 → {model_name: {layer, compiled_path, src_path, sql}}"""
    models = {}
    for p in sorted(COMPILED.glob("*/*.sql")):
        layer = p.parent.name
        name = p.stem
        models[name] = {
            "layer": layer,
            "compiled_path": str(p.relative_to(ROOT)),
            "src_path": str((MODELS_SRC / layer / f"{name}.sql").relative_to(ROOT)),
            "sql": p.read_text(),
        }
    return models


def table_key(e: exp.Table) -> str:
    """qualified Table 节点 → 'schema.table'"""
    return f"{e.db}.{e.name}" if e.db else e.name


# ---------------- 归一化 ----------------
def normalize_condition(cond: exp.Expression) -> str:
    """AST 级归一化:常量折叠、比较方向标准化、去限定名、AND 内排序,输出规范文本。"""
    c = cond.copy()
    try:
        c = simplify(c)
    except Exception:
        pass
    # 比较方向:字面量在左则翻转 (14 >= a → a <= 14)
    FLIP = {exp.GT: exp.LT, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.LTE: exp.GTE}
    for node in list(c.walk()):
        t = type(node)
        if t in FLIP and isinstance(node.this, exp.Literal) and not isinstance(node.expression, exp.Literal):
            new = FLIP[t](this=node.expression, expression=node.this)
            node.replace(new)
    # 列名去表限定(跨模型判等用列名本体)
    for col in list(c.find_all(exp.Column)):
        col.replace(exp.column(col.name))
    s = c.sql(dialect=DIALECT).lower()
    return re.sub(r"\s+", " ", s).strip()


def fingerprint(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:12]


# ---------------- 行号锚定 ----------------
def anchor_line(cond_sql: str, file_text: str) -> int | None:
    """在编译产物文本中定位条件所在行(空白折叠、大小写不敏感、去引号)。"""
    def norm(s):
        return re.sub(r"\s+", " ", s.replace('"', "").replace("_", "").lower()).strip()

    target = norm(cond_sql)
    lines = file_text.splitlines()
    # 先按整行包含匹配,再逐级退化为最具区分度 token 组合(3→2→1)
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
    cte = node.find_ancestor(exp.CTE)
    return cte.alias if cte else "main"


def row_scope_closure(raw_ast: exp.Expression) -> set:
    """从 main 出发,沿 FROM 与 inner join 边可达的 CTE 集合——这些 scope 的条件约束整个行集。
    left join 挂接的 CTE 不在闭包内:其内部条件只影响补列的值,按值路径过滤。"""
    cte_names = {c.alias for c in raw_ast.find_all(exp.CTE)}
    edges = {}
    for sel in raw_ast.find_all(exp.Select):
        sc = scope_name(sel)
        outs = edges.setdefault(sc, [])
        f = sel.args.get("from")
        if f is not None and isinstance(f.this, exp.Table) and f.this.name in cte_names:
            outs.append((f.this.name, "from"))
        for j in sel.args.get("joins") or []:
            jt = j.this
            if isinstance(jt, exp.Table) and jt.name in cte_names:
                kind = (j.side or "inner").lower()
                outs.append((jt.name, kind))
    closure, stack = {"main"}, ["main"]
    while stack:
        cur = stack.pop()
        for child, kind in edges.get(cur, []):
            if kind in ("from", "inner") and child not in closure:
                closure.add(child)
                stack.append(child)
    return closure


def extract_conditions(raw_ast: exp.Expression, file_text: str, model: str):
    """七类口径藏身结构的语义化抽取(基于未 qualify 的原始 AST,便于行号锚定)。"""
    conds, semantics = [], []
    row_scopes = row_scope_closure(raw_ast)

    def add_cond(kind, e, extra=None):
        sql = e.sql(dialect=DIALECT)
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
            jtable = jt.name if isinstance(jt, (exp.Table, exp.Subquery)) else jt.sql(dialect=DIALECT)[:40]
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

    # 窗口惯用法:去重 / 序号
    for w in raw_ast.find_all(exp.Window):
        func = w.this.sql(dialect=DIALECT).lower()
        part = [p.sql(dialect=DIALECT) for p in (w.args.get("partition_by") or [])]
        order = w.args.get("order")
        order_s = order.sql(dialect=DIALECT).replace("ORDER BY ", "") if order else ""
        sel = w.find_ancestor(exp.Select)
        has_eq1 = False
        if sel is not None:
            ql = sel.args.get("qualify")
            if ql and "= 1" in re.sub(r"\s+", " ", ql.sql(dialect=DIALECT)):
                has_eq1 = True
        idiom = ("dedup" if func.startswith("row_number") and has_eq1
                 else "sequence" if func.startswith("row_number")
                 else "window_agg")
        wsql = w.sql(dialect=DIALECT)
        walias = w.find_ancestor(exp.Alias)
        semantics.append({
            "model": model, "scope": scope_name(w), "type": "window", "idiom": idiom,
            "column": walias.alias if walias is not None else None,
            "func": func, "partition_by": part, "order_by": order_s,
            "sql": wsql, "line": anchor_line(wsql, file_text),
        })

    # 表达式级口径点:CASE WHEN / COALESCE(逐 select scope 的投影)
    for sel in raw_ast.find_all(exp.Select):
        for proj in sel.expressions:
            colname = proj.alias_or_name
            for node, tname in ((proj.find(exp.Case), "case_when"), (proj.find(exp.Coalesce), "coalesce")):
                if node is not None:
                    nsql = node.sql(dialect=DIALECT)
                    semantics.append({
                        "model": model, "scope": scope_name(proj), "type": tname,
                        "column": colname, "sql": nsql[:200], "line": anchor_line(nsql, file_text),
                    })
    # 统计日归属:group by 中的日期键与其定义
    for sel in raw_ast.find_all(exp.Select):
        g = sel.args.get("group")
        if not g:
            continue
        proj_by_pos = {str(i + 1): p for i, p in enumerate(sel.expressions)}
        proj_by_name = {p.alias_or_name: p for p in sel.expressions}
        for k in g.expressions:
            ks = k.sql(dialect=DIALECT)
            p = proj_by_pos.get(ks) or proj_by_name.get(ks)
            expr_sql = (p.this if isinstance(p, exp.Alias) else p).sql(dialect=DIALECT) if p is not None else ks
            name = p.alias_or_name if p is not None else ks
            if expr_sql.strip().strip('"') == str(name):
                continue
            if re.search(r"date|dt|day", name.lower()) or "date" in expr_sql.lower():
                semantics.append({
                    "model": model, "scope": scope_name(g), "type": "stat_date_key",
                    "column": name, "sql": expr_sql[:160], "line": anchor_line(expr_sql, file_text),
                })
    return conds, semantics


# ---------------- 图构建 ----------------
def build_graph():
    schema = load_schema()
    models = discover_models()
    graph = {"models": {}, "edges": []}
    for name, m in models.items():
        raw = parse_one(m["sql"], read=DIALECT)
        qast = qualify(raw.copy(), schema=schema, dialect=DIALECT)
        conds, semantics = extract_conditions(raw, m["sql"], name)
        out_cols = [p.alias_or_name for p in qast.expressions]
        col_edges = {}
        for col in out_cols:
            try:
                node = sg_lineage(col, qast, schema=schema, dialect=DIALECT)
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
            proj = next((p for p in qast.expressions if p.alias_or_name == col), None)
            expr_sql = (proj.this if isinstance(proj, exp.Alias) else proj).sql(dialect=DIALECT) if proj is not None else None
            col_edges[col] = {"upstreams": ups, "expr": expr_sql, "scopes": sorted(scopes)}
        graph["models"][name] = {
            "layer": m["layer"], "table": f'{m["layer"]}.{name}',
            "compiled_path": m["compiled_path"], "src_path": m["src_path"],
            "columns": col_edges, "conditions": conds, "semantics": semantics,
        }
    return graph


def main():
    graph = build_graph()
    from lineage.trace import graph_path
    out = graph_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, ensure_ascii=False, indent=1))
    ncols = sum(len(m["columns"]) for m in graph["models"].values())
    nedges = sum(len(c["upstreams"]) for m in graph["models"].values() for c in m["columns"].values())
    nconds = sum(len(m["conditions"]) for m in graph["models"].values())
    nsem = sum(len(m["semantics"]) for m in graph["models"].values())
    errs = [(n, c) for n, m in graph["models"].items() for c, d in m["columns"].items() if d.get("error")]
    print(f"graph: {len(graph['models'])} models, {ncols} columns, {nedges} col-edges, "
          f"{nconds} conditions, {nsem} semantic points -> {out.relative_to(ROOT)}")
    if errs:
        print("column lineage errors:", errs)


if __name__ == "__main__":
    main()
