#!/usr/bin/env python3
"""确定性技术口径组合器(0.8 双写赛马:机器通道的公式作者)。

从模型编译 SQL 出发按作用域逐层展开目标列表达式,跨模型沿血缘边替换,合成
「顶层公式 + 命名子表达式」形态的确定性技术公式;与 LLM 归并公式并行产出
(双写),逐卡比对记入 race 块。赛马期发布权威不切换(仍是 LLM 口径 + 既有
置信分级),组合器产出、分歧与 unsupported 覆盖率作为数据积累,由真实项目
裁决权威归属。

失败模式约束(fail-closed):
- 只输出能从 SQL 证明的组合;不能表达的构造 → status=unsupported + 机器原因,
  绝不输出"看起来对"的猜测——组合器的错误模式必须是「漏而诚实」,不是「编而流畅」。
- 聚合/窗口是组合边界:含聚合的定义不得内联进另一聚合(SUM(SUM(x)) 非法),
  也不得在 join 改变粒度的消费位内联(值的 grain 会被静默丢失)——以命名
  子表达式保留,附定义处 grain 标注。
- 自检(round-trip):组合公式必须通过与 LLM 公式完全相同的链内词表 + 聚合
  锚点校验;叶子源集须与通道一(sqlglot lineage)一致——两条独立实现互证,
  任一不符降 ambiguous,原因入卡。
"""
import re

from sqlglot import exp, parse_one
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

from metriclens.lineage import dialect, table_key

MAX_HOPS = 24       # 作用域/模型递归护栏:超深更可能是环或病理嵌套
INLINE_MAX = 72     # 非聚合定义的内联长度上限,超过则命名保可读
MAX_DEFS = 48       # 命名子表达式规模护栏

_SETOP = getattr(exp, "SetOperation", exp.Union)


class Unsup(Exception):
    """组合器遇到无法确定性表达的构造——诚实放弃,绝不猜。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _display(ast: exp.Expression) -> str:
    return re.sub(r"\s+", " ", ast.sql(dialect=dialect()).replace('"', "")).strip()


def _norm_cmp(text) -> str | None:
    """公式规范化比较形:限定名剥到裸列、小写、行数等价类归一。不可解析 → None。"""
    if not text or not str(text).strip():
        return None
    try:
        ast = parse_one(str(text), read=dialect())
    except Exception:
        return None
    if isinstance(ast, exp.Alias):
        ast = ast.this
    ast = ast.transform(lambda n: exp.column(n.name) if isinstance(n, exp.Column) else n)
    s = re.sub(r"\s+", " ", ast.sql(dialect=dialect()).replace('"', "").lower()).strip()
    s = re.sub(r"\bcount\s*\(\s*1\s*\)|\bsum\s*\(\s*1\s*\)", "count(*)", s)
    # sqlglot 对散文过于宽容(中文词被当匿名函数/列名):剥掉字符串字面量后
    # 仍含非 ASCII 的不是 SQL 公式(字面量里的中文如 status='已支付' 合法)
    if re.search(r"[^\x00-\x7f]", re.sub(r"'[^']*'", "", s)):
        return None
    return s


def _def_ref(name: str) -> exp.Column:
    """def 引用节点:名字含点则拆成 表限定.列 形式,与 _inline 回代键一致。"""
    if "." in name:
        t, _, c = name.partition(".")
        return exp.column(c, table=t)
    return exp.column(name)


class _Composer:
    def __init__(self, project, graph: dict):
        self.project, self.graph = project, graph
        self.rel_models = graph.get("relations", {}).get("models", {})
        self.rel_sources = graph.get("relations", {}).get("sources", {})
        self._name_dup = {}
        for m in graph["models"].values():
            n = m.get("name")
            self._name_dup[n] = self._name_dup.get(n, 0) + 1
        self.root_memo: dict = {}       # uid → 根作用域(解析+qualify 每模型一次)
        self._reset()

    def _reset(self):
        self.defs, self.def_by_key = [], {}
        self.leaves: set = set()
        self.notes: list = []
        self.partial = False            # 叶子集不完整(子查询未展开等),互证降为部分
        self.in_progress: set = set()
        self.model_stack: list = []     # 当前展开所在模型(scope 级 def 的归属标注)

    def _disp(self, uid: str) -> str:
        m = self.graph["models"].get(uid) or {}
        n = m.get("name") or uid
        return n if self._name_dup.get(n, 0) <= 1 else f"{m.get('package')}:{n}"

    # ---------------- 模型 SQL → 根作用域 ----------------
    def _root_scope(self, uid: str):
        if uid not in self.root_memo:
            info = self.graph["models"][uid]
            sql = (self.project.project_dir / info["compiled_path"]).read_text()
            try:
                ast = qualify(parse_one(sql, read=dialect()),
                              schema=self.project.schema, dialect=dialect())
                self.root_memo[uid] = build_scope(ast)
            except Unsup:
                raise
            except Exception as e:
                raise Unsup(f"模型 {self._disp(uid)} qualify 失败: {str(e)[:80]}")
            if self.root_memo[uid] is None:
                raise Unsup(f"模型 {self._disp(uid)} 非 SELECT 结构,无法建作用域")
        return self.root_memo[uid]

    def _rel(self, table: exp.Table) -> str:
        tk = table_key(table)
        try:
            return self.project.complete_rel(tk)
        except ValueError as e:
            raise Unsup(str(e)[:120])

    # ---------------- 作用域内展开 ----------------
    def _scope_grain(self, scope) -> list:
        g = scope.expression.args.get("group")
        if not g:
            return []
        keys, projs = [], scope.expression.expressions
        for e in g.expressions:
            if isinstance(e, exp.Literal) and str(e.this).isdigit():
                i = int(e.this) - 1
                keys.append(projs[i].alias_or_name if 0 <= i < len(projs) else str(e.this))
            elif isinstance(e, exp.Column):
                keys.append(e.name)
            else:
                keys.append(_display(e))
        return keys

    def _expand_scope_col(self, scope, col: str, depth: int) -> tuple:
        """作用域内 col 的投影 → 完全展开的 AST + 定义处 grain。UNION 各分支展开
        文本一致才可用,否则诚实放弃(分支多义,单表达式无法代表)。"""
        e = scope.expression
        if isinstance(e, _SETOP):
            branches = [self._expand_scope_col(b, col, depth + 1)
                        for b in (scope.union_scopes or [])]
            if not branches:
                raise Unsup(f"集合操作缺少分支作用域(列 {col})")
            texts = {_display(a) for a, _ in branches}
            if len(texts) > 1:
                raise Unsup(f"UNION 各分支对列 {col} 的定义不一致,无法单表达式化")
            return branches[0]
        proj = next((p for p in e.expressions if p.alias_or_name == col), None)
        if proj is None:
            raise Unsup(f"作用域投影中找不到列 {col}(星号未展开或动态列)")
        inner = (proj.this if isinstance(proj, exp.Alias) else proj).copy()
        return self._expand(scope, inner, depth, False), self._scope_grain(scope)

    def _expand(self, scope, node: exp.Expression, depth: int, in_agg: bool) -> exp.Expression:
        if depth > MAX_HOPS:
            raise Unsup("展开深度超限(疑似环或病理嵌套)")
        if isinstance(node, (exp.Subquery, exp.Select)):
            note = "标量子查询保留原文未展开(其内部口径不做组合声明)"
            if note not in self.notes:
                self.notes.append(note)
            self.partial = True
            return node
        if isinstance(node, exp.Column):
            return self._expand_column(scope, node, depth, in_agg)
        aggish = isinstance(node, (exp.AggFunc, exp.Window))
        for name, val in list(node.args.items()):
            if isinstance(val, exp.Expression):
                node.set(name, self._expand(scope, val, depth, in_agg or aggish))
            elif isinstance(val, list):
                node.set(name, [self._expand(scope, x, depth, in_agg or aggish)
                                if isinstance(x, exp.Expression) else x for x in val])
        return node

    def _expand_column(self, scope, node: exp.Column, depth: int, in_agg: bool) -> exp.Expression:
        alias = node.table
        if not alias:
            srcs = dict(scope.selected_sources or {}) or dict(scope.sources or {})
            if len(srcs) != 1:
                raise Unsup(f"列 {node.name} 缺少限定名且作用域有多个来源,无法归属")
            alias = next(iter(srcs))
        src = scope.sources.get(alias)
        if src is None:
            if node.db or node.catalog:
                # 直接两段/三段限定引用(未经别名):按物理表处理
                rel_txt = ".".join(p for p in (node.catalog, node.db, alias) if p)
                try:
                    rel = self.project.complete_rel(rel_txt)
                except ValueError as e:
                    raise Unsup(str(e)[:120])
                return self._resolve_rel(rel, node.name, scope, depth, in_agg)
            raise Unsup(f"限定名 {alias} 在作用域中无来源(列 {node.name})")
        if isinstance(src, exp.Table):
            return self._resolve_rel(self._rel(src), node.name, scope, depth, in_agg)
        body, grain = self._expand_scope_col(src, node.name, depth + 1)
        return self._consume(body, None, node.name, scope, in_agg, grain)

    def _resolve_rel(self, rel: str, col: str, scope, depth: int, in_agg: bool) -> exp.Expression:
        up = self.rel_models.get(rel)
        if up and up in self.graph["models"]:
            body, grain = self._expand_model_col(up, col, depth + 1)
            return self._consume(body, up, col, scope, in_agg, grain)
        self.leaves.add((rel, col))
        disp_tbl = self.rel_sources.get(rel) or rel.split(".")[-1]
        return exp.column(col, table=disp_tbl)

    def _expand_model_col(self, uid: str, col: str, depth: int) -> tuple:
        key = (uid, col)
        if key in self.in_progress:
            raise Unsup(f"表达式链存在环(模型 {self._disp(uid)} 自引用/增量回读)")
        self.in_progress.add(key)
        self.model_stack.append(uid)
        try:
            body, _ = self._expand_scope_col(self._root_scope(uid), col, depth)
        finally:
            self.model_stack.pop()
            self.in_progress.discard(key)
        return body, list(self.graph["models"][uid].get("grain") or [])

    def _consume(self, body: exp.Expression, model_uid: str | None, col: str,
                 scope, in_agg: bool, grain: list) -> exp.Expression:
        """消费位决策:内联 or 命名子表达式。聚合/窗口定义不进另一聚合、
        不进 join 改粒度的消费位;非聚合定义按可读性内联。"""
        has_agg = body.find(exp.AggFunc) is not None
        has_win = body.find(exp.Window) is not None
        n_srcs = len(dict(scope.selected_sources or {}) or {"_": 1})
        if has_agg or has_win:
            if in_agg or n_srcs > 1:
                return self._make_def(model_uid, col, body, grain,
                                      "agg" if has_agg else "window", join_ctx=n_srcs > 1)
            return body
        if isinstance(body, (exp.Column, exp.Literal)) or len(_display(body)) <= INLINE_MAX:
            return body
        return self._make_def(model_uid, col, body, grain, "expr", join_ctx=False)

    def _make_def(self, model_uid: str | None, col: str, body: exp.Expression,
                  grain: list, kind: str, join_ctx: bool) -> exp.Column:
        # scope 级 def(model_uid=None)归属当前展开所在模型;与同名模型边界 def 分键
        level = "model" if model_uid else "scope"
        owner = model_uid or (self.model_stack[-1] if self.model_stack else None)
        key = (level, owner or "", col, kind)
        d = self.def_by_key.get(key)
        if d is None:
            if len(self.defs) >= MAX_DEFS:
                raise Unsup("命名子表达式规模超限")
            disp = self._disp(owner) if owner else None
            name = col
            if any(x["name"] == name for x in self.defs):
                name = f"{disp}.{col}" if disp else f"{col}_2"
            n = 2
            while any(x["name"] == name for x in self.defs):
                name = f"{disp}.{col}_{n}" if disp else f"{col}_{n}"
                n += 1
            d = {"name": name, "model": disp, "model_uid": owner, "column": col,
                 "expr": _display(body), "ast": body, "grain": grain or [],
                 "kind": kind, "join_context": join_ctx}
            self.defs.append(d)
            self.def_by_key[key] = d
        return _def_ref(d["name"])

    # ---------------- 目标合成 ----------------
    def compose_target(self, uid: str, col: str) -> dict:
        self._reset()
        self.model_stack.append(uid)
        try:
            top, _ = self._expand_scope_col(self._root_scope(uid), col, 0)
        except Unsup as e:
            return {"status": "unsupported", "top": None, "defs": [], "inline": None,
                    "leaves": [], "reasons": [e.reason], "notes": list(self.notes)}
        except Exception as e:  # 组合器内部错误也必须封闭:宁可 unsupported,不出错误公式
            return {"status": "unsupported", "top": None, "defs": [], "inline": None,
                    "leaves": [], "reasons": [f"internal:{type(e).__name__}: {str(e)[:100]}"],
                    "notes": list(self.notes)}
        inline, inline_valid = self._inline(top)
        out_defs = [{k: d[k] for k in ("name", "model", "column", "expr", "grain",
                                       "kind", "join_context")} for d in self.defs]
        return {
            "status": "ambiguous" if (self.notes or self.partial) else "proven",
            "top": _display(top), "defs": out_defs,
            "inline": inline if inline_valid else None,
            "inline_cmp": inline,     # 比较候选:含嵌套聚合的纯文本形也参与匹配
            "leaves": sorted(f"{r}.{c}" for r, c in self.leaves),
            "leaf_pairs": sorted(self.leaves),
            "partial_leaves": self.partial,
            "reasons": list(self.notes), "notes": list(self.notes),
        }

    def _inline(self, top: exp.Expression) -> tuple:
        """def 引用全量回代的单表达式(机器比较候选);嵌套聚合出现则标记
        不可作展示公式(inline=None),文本形仍供比较。"""
        ast, valid = top.copy(), True
        by_ref = {}
        for d in self.defs:
            r = _def_ref(d["name"])
            by_ref[(r.table or None, r.name)] = d

        for _ in range(10):
            hit = False

            def sub(n):
                nonlocal hit
                if isinstance(n, exp.Column):
                    d = (by_ref.get((n.table, n.name)) if n.table
                         else by_ref.get((None, n.name)))
                    if d is not None:
                        hit = True
                        return d["ast"].copy()
                return n

            ast = ast.transform(sub)
            if not hit:
                break
        # 嵌套聚合(SUM(SUM(x)))非法 SQL:回代形不可作展示公式,仅供文本比较
        for agg in ast.find_all(exp.AggFunc):
            if any(x is not agg for x in agg.find_all(exp.AggFunc)):
                valid = False
                break
        return _display(ast), valid


# ---------------- 逐事实块 ----------------
def build_facts(project, graph: dict, t: dict, targets: list,
                chain_vocab: tuple, link_aggs: set) -> dict:
    """逐事实技术口径:formula(组合器合成)/ key_filters / sources / window /
    grain,均带 status 与机器原因。除 formula 外都是通道一既有确定性产物的
    显式归档(它们本就由机器作者)。"""
    from metriclens.synth import formula_agg_check, verify_freetext

    comp = _Composer(project, graph)
    per_target = []
    for uid, col in targets:
        c = comp.compose_target(uid, col)
        c["target"] = f"{comp._disp(uid)}.{col}"
        per_target.append(c)

    multi = len(per_target) > 1
    f0 = per_target[0]
    formula = {
        "status": f0["status"], "top": f0["top"], "defs": f0["defs"],
        "inline": f0["inline"], "inline_cmp": f0.get("inline_cmp"),
        "reasons": list(f0["reasons"]),
    }
    if multi:
        formula = {
            "status": "ambiguous" if all(p["status"] != "unsupported" for p in per_target)
            else "unsupported",
            "top": None, "defs": [], "inline": None,
            "per_target": [{k: p.get(k) for k in ("target", "status", "top", "defs",
                                                  "inline", "inline_cmp", "reasons")}
                           for p in per_target],
            "reasons": ["多目标指标:目标间组合关系由配置/业务口径声明,非单一 SQL 事实"]
                       + [r for p in per_target for r in p["reasons"]],
        }

    # round-trip 自检:与 LLM 公式完全相同的校验器 + 叶子源集互证。
    # 失败 = 组合器与通道一血缘/词表两条独立实现不一致——机器侧内部矛盾,
    # 结构化标记(rt_failed),发布状态机据此阻断 VERIFIED。
    formula["rt_failed"] = False
    if formula["status"] != "unsupported":
        ok_targets = [p for p in per_target if p["status"] != "unsupported"]
        closure = " ".join(
            [p["top"] or "" for p in ok_targets]
            + [d["expr"] for p in ok_targets for d in p["defs"]])
        c_idents, c_nums = chain_vocab
        bad = verify_freetext(closure, c_idents, c_nums)
        if bad:
            formula["status"], formula["rt_failed"] = "ambiguous", True
            formula["reasons"].append(f"round-trip:组合公式含词表外 token {bad[:4]}")
        agg_bad = formula_agg_check(closure, link_aggs)
        if agg_bad:
            formula["status"], formula["rt_failed"] = "ambiguous", True
            formula["reasons"].append(f"round-trip:聚合锚点不符 {agg_bad[:2]}")
        if not any(p["partial_leaves"] for p in ok_targets):
            mismatch = _leaf_mismatch(graph, t, ok_targets)
            if mismatch:
                formula["status"], formula["rt_failed"] = "ambiguous", True
                formula["reasons"].append(f"round-trip:叶子源集与通道一血缘不符 {mismatch}")
        else:
            formula["reasons"].append("叶子源集互证部分跳过(存在未展开子查询)")

    amb = t.get("scope_ambiguous") or []
    amb_conds = [{"sql": str(c.get("sql", ""))[:120], "model": c.get("model"),
                  "scope": c.get("scope"), "line": c.get("line")}
                 for c in amb if "type" not in c]
    key_items = [{"sql": c["sql"], "kind": c.get("kind"), "model": c.get("model"),
                  "line": c.get("line")}
                 for c in t["conditions"] if not c.get("is_pure_key")]
    facts = {
        "formula": formula,
        "key_filters": {"status": "ambiguous" if amb else "proven",
                        "items": key_items, "ambiguous_items": amb_conds,
                        "reasons": (["存在别名复用 scope 的归因不明条件,过滤事实不完整"]
                                    if amb else [])},
        "sources": {"status": "proven",
                    "items": [dict(s) for s in t["sources"]],
                    "reasons": []},
        "window": {"status": "proven",
                   "items": [{"sql": s.get("sql"), "idiom": s.get("idiom"),
                              "model": s.get("model"), "line": s.get("line"),
                              "partition_by": s.get("partition_by"),
                              "order_by": s.get("order_by")}
                             for s in t["semantics"] if s.get("type") == "window"],
                   "unique_on": {comp._disp(mo): graph["models"][mo]["unique_on"]
                                 for mo in t["models_visited"]
                                 if graph["models"].get(mo, {}).get("unique_on")},
                   "reasons": []},
        "grain": _grain_fact(graph, t),
    }
    return facts


def _grain_fact(graph: dict, t: dict) -> dict:
    for e in t["expr_chain"]:
        g = graph["models"].get(e["model_uid"], {}).get("grain")
        if g:
            return {"status": "proven", "keys": list(g), "model": e["model"], "reasons": []}
    return {"status": "unknown", "keys": [],
            "reasons": ["值链上未发现分组粒度(明细直取或粒度藏于不可见层)"]}


def _leaf_mismatch(graph: dict, t: dict, per_target: list) -> str | None:
    """组合器叶子 vs 通道一 sources:两条独立实现(作用域展开 vs sqlglot lineage)
    的源集互证。star(表级)源只要求表命中。"""
    rel_sources = graph.get("relations", {}).get("sources", {})
    mine = set()
    for p in per_target:
        for rel, col in p["leaf_pairs"]:
            parts = str(rel).split(".")
            if len(parts) >= 3:
                db, sch, tail = parts[0], parts[1], ".".join(parts[2:])
            elif len(parts) == 2:
                db, sch, tail = "", parts[0], parts[1]
            else:
                db, sch, tail = "", "", parts[0]
            disp = rel_sources.get(rel) or tail
            mine.add((db, sch, disp, col))
    theirs = {(s.get("database") or "", s.get("schema") or "", s["table"], s["column"])
              for s in t["sources"] if s["column"] != "*"}
    star_tbls = {(s.get("database") or "", s.get("schema") or "", s["table"])
                 for s in t["sources"] if s["column"] == "*"}
    extra = {m for m in mine - theirs if m[:3] not in star_tbls}
    missing = theirs - mine
    if not extra and not missing:
        return None
    brief = []
    if missing:
        brief.append("通道一有而组合器未达: " + ", ".join(
            ".".join(x for x in m if x) for m in sorted(missing)[:3]))
    if extra:
        brief.append("组合器多出: " + ", ".join(
            ".".join(x for x in m if x) for m in sorted(extra)[:3]))
    return "; ".join(brief)


def attach_evidence(facts: dict, evidence: list) -> None:
    """把确定性证据清单的 ID 回挂到逐事实块(与 build_evidence 同键规则)。"""
    from metriclens.synth import norm_text
    by_key = {(e["kind"], norm_text(e["text"])): e["id"] for e in evidence}
    facts["formula"]["evidence"] = sorted(
        e["id"] for e in evidence if e["kind"] == "expression")
    for it in facts["key_filters"]["items"]:
        eid = by_key.get((f"condition:{it.get('kind', '')}", norm_text(it["sql"])))
        if eid:
            it["evidence"] = eid
    for it in facts["window"]["items"]:
        eid = by_key.get(("semantic:window", norm_text(it.get("sql"))))
        if eid:
            it["evidence"] = eid


# ---------------- 赛马比对与发布状态 ----------------
def race_formula(facts: dict, llm_formula, contradictions: dict | None) -> dict:
    """公式双写比对。verdict:
    renderer_unsupported 组合器覆盖不了(覆盖率数据,非分歧)
    disagree             机器矛盾实锤(聚合锚点/链内词表,与置信降级同源)
    prose                LLM 公式不可解析为 SQL(仅 token 级校验兜底)
    agree                规范化后与组合器某一展开形结构一致
    consistent           无机器矛盾但未达结构一致(粒度不同的合法表述等)"""
    f = facts["formula"]
    if f["status"] == "unsupported":
        return {"verdict": "renderer_unsupported", "detail": {"reasons": f["reasons"][:4]}}
    cons = {k: v for k, v in (contradictions or {}).items() if v}
    if cons:
        return {"verdict": "disagree", "detail": cons}
    cands = set()

    def _add_target_cands(p):
        for c in (p.get("top"), p.get("inline"), p.get("inline_cmp")):
            n = _norm_cmp(c)
            if n:
                cands.add(n)
        # 纯直通指标(top 就是单个 def 名):LLM 天然在 def 的粒度上写公式,
        # 该 def 的定义体也是合法比对候选(顶层壳不携带口径内容)
        tn = _norm_cmp(p.get("top"))
        if tn and re.fullmatch(r"[a-z_][a-z0-9_.]*", tn):
            for d in p.get("defs") or []:
                if _norm_cmp(d["name"]) == tn:
                    n = _norm_cmp(d["expr"])
                    if n:
                        cands.add(n)
    _add_target_cands(f)
    for p in f.get("per_target") or []:
        _add_target_cands(p)
    llm_n = _norm_cmp(llm_formula)
    if llm_n is None:
        return {"verdict": "prose",
                "detail": {"note": "LLM 公式非可解析 SQL,结构比对不适用(词表/锚点校验已过)"}}
    if llm_n in cands:
        return {"verdict": "agree", "detail": {"matched": llm_n[:120]}}
    return {"verdict": "consistent",
            "detail": {"note": "无机器矛盾但未达结构一致", "llm": llm_n[:120],
                       "renderer": sorted(cands)[0][:120] if cands else None}}


def publication_status(confidence: str, facts: dict, race: dict) -> str:
    """发布状态机(赛马期语义,置信分级不动):
    REVIEW_REQUIRED  双通道公式实锤矛盾 / 过滤事实归因不明 / round-trip 自检
                     失败(机器两条实现互相矛盾)——须人工
    VERIFIED         互验高置信且无机器矛盾
    TECHNICAL_ONLY   机器公式自证成立但 LLM 表述未过互验——技术事实可用,叙述待审
    (BLOCKED 保留:目标不可解析等硬失败当前直接报错,不落卡。)"""
    if (race.get("verdict") == "disagree"
            or facts["key_filters"]["status"] == "ambiguous"
            or facts["formula"].get("rt_failed")):
        return "REVIEW_REQUIRED"
    if confidence == "high":
        return "VERIFIED"
    if facts["formula"]["status"] == "proven":
        return "TECHNICAL_ONLY"
    return "REVIEW_REQUIRED"
