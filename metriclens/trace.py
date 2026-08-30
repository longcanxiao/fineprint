#!/usr/bin/env python3
"""反向遍历:从任一模型列出发回溯至源表,收敛 S(源字段)/F(过滤条件)/E(表达式链) 三元组。"""
import hashlib
import json
from pathlib import Path

from metriclens.lineage import set_dialect


def load_graph(path: Path) -> dict:
    raw = Path(path).read_bytes()
    g = json.loads(raw)
    ver = g.get("meta", {}).get("metriclens_graph_version", 0)
    if ver < 3:
        raise ValueError(
            f"血缘图版本过旧(v{ver}):0.7 起身份体系升级为 unique_id 主键 + 物理三段反查键;"
            f"请重新执行 metriclens graph(图是派生物,重建零成本)")
    # 图文件指纹:卡片/治理报告/漂移快照据此绑定生成时的图,验收可检出混版本产物
    g.setdefault("meta", {})["graph_md5"] = hashlib.md5(raw).hexdigest()[:16]
    set_dialect(g.get("meta", {}).get("dialect", "duckdb"))
    return g


def display_name(graph: dict, uid: str) -> str:
    """模型展示名:短名唯一用短名,重名用 pkg:name;未知 uid 原样返回。"""
    m = graph["models"].get(uid)
    if m is None:
        return uid
    name = m.get("name") or uid
    dup = sum(1 for x in graph["models"].values() if x.get("name") == name)
    return name if dup <= 1 else f'{m.get("package")}:{name}'


def resolve_model(graph: dict, ref: str) -> str:
    """模型引用 → unique_id。接受:完整 uid / 短名(须唯一)/ pkg:name / pkg.name。
    歧义与未知都显式报错——短名只是 UI,绝不静默选一个。"""
    models = graph["models"]
    if ref in models:
        return ref
    pkg, nm = None, None
    if ":" in ref:
        pkg, _, nm = ref.partition(":")
    elif "." in ref:
        parts = ref.split(".")
        if len(parts) == 2:
            pkg, nm = parts
    else:
        nm = ref
    if nm:
        hits = [u for u, m in models.items()
                if m.get("name") == nm and (pkg is None or m.get("package") == pkg)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            cands = ", ".join(f'{models[u].get("package")}.{nm}' for u in sorted(hits))
            raise KeyError(f"模型名有歧义: {ref}(候选: {cands};请用 package.model 消歧)")
    external = graph.get("relations", {}).get("external", {})
    pkgs = sorted({e["package"] for e in external.values() if e.get("name") == (nm or ref)})
    hint = (f"(属第三方包 {'/'.join(pkgs)},按数据源边界处理,不解析其内部口径;"
            f"如需为其出卡,把包名加入 metriclens.yml 顶层 internal_packages 并重建图)"
            if pkgs else "")
    raise KeyError(f"unknown model: {ref}{hint}")


def trace(graph: dict, model: str, column: str) -> dict:
    models = graph["models"]
    model_rel = graph.get("relations", {}).get("models", {})
    source_rel = graph.get("relations", {}).get("sources", {})
    external = graph.get("relations", {}).get("external", {})
    model = resolve_model(graph, model)
    disp_cache: dict = {}

    def disp(uid: str) -> str:
        if uid not in disp_cache:
            disp_cache[uid] = display_name(graph, uid)
        return disp_cache[uid]

    if column not in models[model]["columns"]:
        if models[model].get("error"):
            raise KeyError(f"模型 {disp(model)} 解析失败,无法回溯其列 {column}"
                           f"({models[model]['error']})")
        raise KeyError(f"unknown column: {disp(model)}.{column} "
                       f"(有效列: {list(models[model]['columns'])[:8]}...)")

    sources, chain, visited = [], [], set()
    visited_models = []
    model_scopes: dict[str, set] = {}
    source_rels: set = set()          # 值链源表的原始 rel(上下文计算按 rel 排除)

    def add_source(rel: str, col: str):
        source_rels.add(rel)
        # table 保留裸名(展示/LLM 互验/文档匹配用);schema/database 单独携带,
        # 供治理指纹与漂移快照区分跨 schema/跨库同名源表(erp.orders vs crm.orders)
        parts = rel.split(".")
        if len(parts) >= 3:
            db, sch, tbl = parts[0], parts[1], ".".join(parts[2:])
        elif len(parts) == 2:
            db, sch, tbl = "", parts[0], parts[1]
        else:
            db, sch, tbl = "", "", rel
        key = {"table": source_rel.get(rel) or tbl, "schema": sch, "database": db, "column": col}
        pkg = (external.get(rel) or {}).get("package")
        if pkg:
            key["package"] = pkg   # 第三方包物化表:标注归属,卡片上可见边界性质
        if key not in sources:
            sources.append(key)

    def rec_rowset(m: str):
        """表级行集访问(COUNT(*) 类断链兜底):收该模型的行集条件并继续沿行集表回溯,
        不进表达式链;model_scopes 留空集 → 只有 row_level 条件生效。"""
        if ("*", m) in visited:
            return
        visited.add(("*", m))
        if m not in visited_models:
            visited_models.append(m)
        model_scopes.setdefault(m, set())
        if models[m].get("error"):
            add_source(models[m]["table"], "*")   # 解析失败模型:行集依赖在其物化表截止
            return
        for rel in models[m].get("row_set_tables") or []:
            up_model = model_rel.get(rel)
            if up_model and up_model in models:
                rec_rowset(up_model)
            else:
                add_source(rel, "*")

    def rec(m: str, c: str, depth: int):
        if (m, c) in visited:
            return
        visited.add((m, c))
        if m not in visited_models:
            visited_models.append(m)
        entry = models[m]["columns"][c]
        model_scopes.setdefault(m, set()).update(entry.get("scopes", ["main"]))
        chain.append({
            # model = 展示名(文档/漂移快照/LLM 按它工作),model_uid = 图主键(机器反查)
            "depth": depth, "layer": models[m]["layer"], "model": disp(m), "model_uid": m,
            "column": c, "expr": entry.get("expr"), "src_path": models[m]["src_path"],
        })
        for up in entry.get("upstreams", []):
            rel = up["table"]
            up_model = model_rel.get(rel)
            if up["column"] == "*":
                if up_model and up_model in models:
                    rec_rowset(up_model)
                else:
                    add_source(rel, "*")
            elif up_model and up_model in models:
                if up["column"] in models[up_model].get("columns", {}):
                    rec(up_model, up["column"], depth + 1)
                else:
                    # 上游模型解析失败(或列未知):血缘在其物化表处截止,按边界源记账
                    add_source(rel, up["column"])
            else:
                add_source(rel, up["column"])

    rec(model, column, 0)

    conds, sems, ambiguous, seen_fp = [], [], [], set()
    for m in visited_models:
        scopes = model_scopes.get(m, {"main"})
        for c in models[m]["conditions"]:
            # 行集条件(main/FROM/inner join 闭包内)对全列生效——行集闭包在建图期用
            # 唯一 scope 名计算,归因可信;其余按值路径 scope 过滤(scope 唯一名可能带
            # @n 消歧后缀,列级 scopes 是裸别名,按 base 比对)
            if not c.get("row_level"):
                if c["scope"].split("@")[0] not in scopes:
                    continue
                if c.get("scope_dup"):
                    # 别名复用:裸别名命中无法证明列路径经过的是"这一个"同名 scope,
                    # 不归因进口径,单独暴露供人工确认(归因错误比缺失更危险)
                    ambiguous.append({**c, "src_path": models[m]["src_path"]})
                    continue
            if c["fp"] in seen_fp:
                continue
            seen_fp.add(c["fp"])
            conds.append({**c, "src_path": models[m]["src_path"]})
        for s in models[m]["semantics"]:
            if s.get("scope", "main").split("@")[0] not in scopes:
                continue
            if s.get("column") and (m, s["column"]) not in visited and s["type"] != "stat_date_key":
                continue
            if s.get("scope_dup"):
                ambiguous.append({**s, "src_path": models[m]["src_path"]})
                continue
            sems.append({**s, "src_path": models[m]["src_path"]})

    # join/分组上下文表(通道一的第二类视野):途经模型行集与全向 join 闭包内的
    # 表,去掉值链源表、链上模型自身及其物化表——它们不供值,但塑造行集与粒度
    # (分组维表、join 伙伴)。通道二解释口径时引用它们是合法上下文而非幻觉;
    # 互验据此把这类引用与值源分开记账(s_context_by_llm),模型上下文携带列清单
    # 供列存在性校验。仅作可见化,不进指纹/漂移快照。
    own_tables = {models[m]["table"] for m in visited_models if m in models}
    ctx: dict = {}
    for m in visited_models:
        info = models.get(m) or {}
        rels = list(info.get("row_set_tables") or []) \
            + [e["rel"] for e in info.get("row_risk_joins") or []]
        for rel in rels:
            if rel in source_rels or rel in own_tables or rel in ctx:
                continue
            up = model_rel.get(rel)
            if up and up in models:
                if up in visited_models:
                    continue          # 链上模型:已在值链视野,非上下文
                ctx[rel] = {"table": rel, "model": disp(up), "model_uid": up,
                            "columns": sorted(c.lower() for c in models[up].get("columns") or {})}
            else:
                pkg = (external.get(rel) or {}).get("package")
                ctx[rel] = {"table": rel, **({"package": pkg} if pkg else {})}

    return {
        "target": f"{models[model]['layer']}.{disp(model)}.{column}",
        "depth": 1 + max((e["depth"] for e in chain), default=0),
        # models_visited 存 uid(graph["models"] 的机器可用键);展示走 expr_chain 的 model 字段
        "models_visited": visited_models,
        "sources": sorted(sources, key=lambda x: (x["table"], x["column"], x.get("schema", ""))),
        "context_tables": [ctx[k] for k in sorted(ctx)],
        "expr_chain": chain,
        "conditions": conds,
        "semantics": sems,
        "scope_ambiguous": ambiguous,
    }


def render(t: dict) -> str:
    L = []
    L.append(f"◎ 目标: {t['target']}   (链路 {t['depth']} 层,经过 {len(t['models_visited'])} 个模型)")
    L.append("\n── 表达式链 E ──")
    for e in t["expr_chain"]:
        pad = "  " * e["depth"]
        expr = (e["expr"] or "").replace('"', "")
        if len(expr) > 96:
            expr = expr[:96] + "…"
        L.append(f"{pad}[{e['layer']}] {e['model']}.{e['column']} = {expr}")
    L.append(f"\n── 源字段 S ({len(t['sources'])} 个) ──")
    for s in t["sources"]:
        tag = f"   ⟵ 第三方包 {s['package']}(数据源边界,内部口径不解析)" if s.get("package") else ""
        L.append(f"  {s['table']}.{s['column']}{tag}")
    key_conds = [c for c in t["conditions"] if not c.get("is_pure_key")]
    pure = [c for c in t["conditions"] if c.get("is_pure_key")]
    L.append(f"\n── 过滤条件 F ({len(key_conds)} 条业务条件 + {len(pure)} 条纯关联键) ──")
    for c in key_conds:
        jt = f" join({c.get('join_table')})" if c["kind"] == "join_on" else ""
        L.append(f"  [{c['kind']}{jt}] {c['sql'].replace(chr(34), '')}"
                 f"\n      ↳ {c['src_path']} · {c['model']}({c['scope']}) · 编译行 L{c['line']}")
    amb = t.get("scope_ambiguous") or []
    if amb:
        L.append(f"\n── 归因不明 ({len(amb)},别名复用 scope,须人工确认) ──")
        for c in amb:
            L.append(f"  ? {str(c.get('sql', '')).replace(chr(34), '')[:80]}"
                     f"   ↳ {c.get('model')}({c.get('scope')}) L{c.get('line')}")
    L.append(f"\n── 结构语义点 ({len(t['semantics'])}) ──")
    for s in t["semantics"]:
        desc = {
            "window": lambda s: f"窗口/{s['idiom']}: {s['sql'].replace(chr(34), '')}"
                      + (f"  → 按 {','.join(s['partition_by'])} 取 {s['order_by']} 首行" if s["idiom"] == "dedup" else ""),
            "case_when": lambda s: f"CASE WHEN → {s.get('column')}: {s['sql'][:80].replace(chr(34), '')}",
            "coalesce": lambda s: f"COALESCE 兜底 → {s.get('column')}: {s['sql'][:80].replace(chr(34), '')}",
            "stat_date_key": lambda s: f"统计日归属 → {s.get('column')} = {s['sql'].replace(chr(34), '')}",
            "join_count": lambda s: (f"⚠ 行数聚合跨 join → {s.get('column')}: 行集由 "
                                     f"{' ⋈ '.join(s.get('tables', []))} 匹配结构决定,计数对象未自证(质量治理项)"),
        }[s["type"]](s)
        L.append(f"  [{s['model']}] {desc}  @L{s['line']}")
    return "\n".join(L)
