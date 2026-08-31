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

from fineprint.lineage import dialect, open_world_tables, table_key

MAX_HOPS = 512      # 纯兜底护栏:环由模型级/作用域级 in_progress 显式防护;
                    # 真实深度可观(Fivetran ad_reporting 跨源 rollup 实测 257 层,
                    # 代码生成的链式 CTE × inline ephemeral × 跨模型累计)
INLINE_MAX = 72     # 非聚合定义的内联长度上限,超过则命名保可读
MAX_DEFS = 200      # 命名子表达式规模护栏(可读性参数,非正确性;date-spine ×
                    # 多结构体的真实模型可达 50+,展示层自行截断)

_SETOP = getattr(exp, "SetOperation", exp.Union)


class Unsup(Exception):
    """组合器遇到无法确定性表达的构造——诚实放弃,绝不猜。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _flatten_types() -> tuple:
    """FLATTEN 族伪来源节点类型(TableFromRows 仅新版 sqlglot 有)。"""
    t = getattr(exp, "TableFromRows", None)
    return (t,) if t else ()


def _display(ast: exp.Expression) -> str:
    # 展示形去引号:双引号(通用)与反引号(bigquery/mysql 系)都属噪声
    return re.sub(r"\s+", " ", ast.sql(dialect=dialect()).replace('"', "").replace("`", "")).strip()


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
    s = re.sub(r"\s+", " ",
               ast.sql(dialect=dialect()).replace('"', "").replace("`", "").lower()).strip()
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
        self._corpus_claims: dict | None = None   # rel(小写) → 语料担保列集,惰性挖掘
        self._reset()

    def _corpus_claim(self, rel: str, col: str) -> bool:
        """语料限定声明:项目中任何已归属的 (表,列) 血缘边都是该表有该列的证据
        (显式 T.col 或单源作用域,均由 SQL 语义担保)。多源裸列若语料声明者唯一
        即可归属——这些 SQL 在生产运行,而 SQL 对多来源同名裸列报 ambiguous 错,
        查询能跑本身就证明作用域内恰有一张表有该列。"""
        if self._corpus_claims is None:
            cc: dict = {}
            for m in self.graph["models"].values():
                for c in (m.get("columns") or {}).values():
                    for u in c.get("upstreams") or []:
                        t, cn = u.get("table"), (u.get("column") or "").lower()
                        if t and cn:
                            cc.setdefault(t.lower(), set()).add(cn)
            self._corpus_claims = cc
        return col.lower() in self._corpus_claims.get(rel.lower(), ())

    def _reset(self):
        self.defs, self.def_by_key = [], {}
        self.leaves: set = set()
        self.notes: list = []
        self.partial = False            # 叶子集不完整(子查询未展开等),互证降为部分
        self.in_progress: set = set()
        self.scope_stack: set = set()   # (scope id, col) 展开中:递归 CTE 环守卫
        self.model_stack: list = []     # 当前展开所在模型(scope 级 def 的归属标注)

    def _disp(self, uid: str) -> str:
        m = self.graph["models"].get(uid) or {}
        n = m.get("name") or uid
        return n if self._name_dup.get(n, 0) <= 1 else f"{m.get('package')}:{n}"

    # ---------------- 模型 SQL → 根作用域 ----------------
    def _root_scope(self, uid: str):
        if uid not in self.root_memo:
            info = self.graph["models"][uid]
            sql = (self.project.project_dir / info["compiled_path"]).read_text(encoding="utf-8")
            try:
                ast = parse_one(sql, read=dialect())
            except Exception as e:
                raise Unsup(f"模型 {self._disp(uid)} parse 失败: {str(e)[:80]}")
            # qualify 的星号展开会按 schema 重写投影;先在原始 AST 上记住哪些
            # SELECT 真的写过星号——开放世界直通规则只对"星号被展开吃掉"的
            # 作用域放行,显式投影缺列仍严格拒绝
            for sel in ast.find_all(exp.Select):
                if any(isinstance(p, exp.Star)
                       or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
                       for p in sel.expressions):
                    sel.meta["had_star"] = True
            try:
                qast = qualify(ast.copy(), schema=self.project.schema, dialect=dialect())
            except Unsup:
                raise
            except Exception:
                # 与建图同策略:退部分限定,定不了的列留裸名,由展开期
                # 「缺少限定名」逐列诚实报错——不因一列废整模型;开放世界
                # 模型再放开已限定列的列集校验(见 open_world_tables)
                try:
                    qast = qualify(ast.copy(), schema=self.project.schema,
                                   dialect=dialect(), validate_qualify_columns=False,
                                   allow_partial_qualification=open_world_tables(
                                       ast, self.project))
                except Exception as e:
                    raise Unsup(f"模型 {self._disp(uid)} qualify 失败: {str(e)[:80]}")
            self.root_memo[uid] = build_scope(qast)
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
        一致则取其一;异构分支(跨源 rollup 的常态)逐分支保留为 union def——
        「值 = 行所来自分支的表达式」本身就是可证事实,不放弃也不挑一支。
        集合操作按 SQL 语义**位置对齐**:列位取自首分支,后续分支可不命名
        (dbt_utils date_spine 的 `select 0 as n union all select 1` 惯用法)。"""
        e = scope.expression
        # 派生表/CTE 列别名清单:from (…) as t(c1,c2) / with t(c1,c2) as (…) ——
        # 外部列名按清单位置映射到内部投影(内部常是字面量自动命名)
        apos = None
        acols = self._alias_columns(scope)
        if acols:
            low = [c.lower() for c in acols]
            if col.lower() in low:
                apos = low.index(col.lower())
        if isinstance(e, _SETOP):
            # 链式 union 在 AST 里是嵌套二叉结构:递归拍平成 Select 分支序列,
            # 位置对齐才有意义(首分支命名,后续分支可裸写字面量)
            uscopes = self._flat_union_scopes(scope)
            if not uscopes:
                raise Unsup(f"集合操作缺少分支作用域(列 {col})")
            first = uscopes[0].expression.expressions
            pos = apos if apos is not None else next(
                (i for i, p in enumerate(first) if p.alias_or_name == col), None)
            branches = []
            for b in uscopes:
                exprs = b.expression.expressions
                proj = (exprs[pos] if pos is not None and pos < len(exprs)
                        else next((x for x in exprs if x.alias_or_name == col), None))
                if proj is None:
                    branches.append(self._expand_scope_col(b, col, depth + 1))
                else:
                    branches.append(self._expand_proj(b, proj, col, depth + 1))
            texts = {_display(a) for a, _ in branches}
            if len(texts) == 1:
                return branches[0]
            labels = [self._branch_label(b) for b in uscopes]
            return self._make_union_def(scope, col, branches, labels), []
        if apos is not None and apos < len(e.expressions):
            return self._expand_proj(scope, e.expressions[apos], col, depth)
        proj = next((p for p in e.expressions if p.alias_or_name == col), None)
        if proj is None:
            # 星号直通:qualify 因无 schema 未展开星号(未编目表/表值函数)——
            # 列按裸引用在本作用域降级解析;星号带唯一限定名则归属该源
            stars = []          # (限定名或 None, except 排除集)
            for p in e.expressions:
                if isinstance(p, exp.Star):
                    stars.append((None, {c.name.lower() for c in p.args.get("except") or []}))
                elif isinstance(p, exp.Column) and isinstance(p.this, exp.Star):
                    stars.append((p.table or None,
                                  {c.name.lower() for c in p.this.args.get("except") or []}))
            claiming = [q for q, exc in stars if col.lower() not in exc]
            if claiming:
                # t.* EXCEPT(col) 的星号不担保被排除列;唯一担保星号带限定名则归属
                # 该源,否则按裸列在本作用域解析(单源/担保裁决在 _expand_column)
                ref = (exp.column(col, table=claiming[0])
                       if len(claiming) == 1 and claiming[0] else exp.column(col))
                return self._expand(scope, ref, depth, False), self._scope_grain(scope)
            hit = self._pivot_of(scope, None)
            if hit is not None:
                # FROM 带 PIVOT 而投影缺列:按透视输出列确定性改写
                # (agg(case when field=value)),id 列直通输入作用域
                return self._pivot_resolve(scope, *hit, col, depth, False), []
            parent = scope.expression.parent
            if (scope.expression.find(exp.Pivot) is not None
                    or (parent is not None and parent.args.get("pivots"))):
                raise Unsup(f"PIVOT 输出列展开未支持(列 {col},非常规形态)")
            # 开放世界表:qualify 用 yml 部分声明展开并吃掉了星号(原始 SQL 确
            # 有星号,meta 为证),未声明列在展开后的投影里自然缺席。单源真实表
            # 且不在 catalog(闭世界实测)时,一方 SQL 的显式引用不因文档只写了
            # 子集而拒——按裸列降级解析(与通道一 lenient 语义一致);catalog
            # 编目过的表与显式投影仍严格拒绝。
            osrcs = list((scope.sources or {}).values())
            if ((getattr(e, "_meta", None) or {}).get("had_star")
                    and len(osrcs) == 1 and isinstance(osrcs[0], exp.Table)
                    and self.project.complete_rel(table_key(osrcs[0]))
                    not in self.project.catalog_rels):
                return (self._expand(scope, exp.column(col), depth, False),
                        self._scope_grain(scope))
            raise Unsup(f"作用域投影中找不到列 {col}(星号未展开或动态列)")
        return self._expand_proj(scope, proj, col, depth)

    def _flat_union_scopes(self, scope) -> list:
        out = []
        for b in scope.union_scopes or []:
            if isinstance(b.expression, _SETOP):
                out.extend(self._flat_union_scopes(b))
            else:
                out.append(b)
        return out

    @staticmethod
    def _alias_columns(scope) -> list:
        """派生表/CTE 的列别名清单(as t(c1, c2, …));无则空列表。"""
        p = scope.expression.parent
        alias = p.args.get("alias") if p is not None else None
        cols = getattr(alias, "columns", None) if alias is not None else None
        return [str(c.name) for c in cols or []]

    def _expand_proj(self, scope, proj, col: str, depth: int) -> tuple:
        skey = (id(scope), col)
        if skey in self.scope_stack:
            raise Unsup(f"作用域自引用(递归 CTE?)——列 {col} 的定义回到自身")
        self.scope_stack.add(skey)
        try:
            inner = (proj.this if isinstance(proj, exp.Alias) else proj).copy()
            return self._expand(scope, inner, depth, False), self._scope_grain(scope)
        finally:
            self.scope_stack.discard(skey)

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
        if isinstance(node, exp.Dot):
            # 结构体字段访问 base.field(BigQuery 嵌套列):展开基表达式,
            # 字段路径原样保留——字段语义寄生在基列上,叶子记账记基列(与通道一对齐)
            base = self._expand(scope, node.this.copy(), depth, in_agg)
            return exp.Dot(this=base, expression=node.expression.copy())
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
            if len(srcs) == 1:
                alias = next(iter(srcs))
            else:
                # 多源裸列按 SQL 名解析:唯一担保源归属;USING 列各侧等值,
                # 取最左携带源;仍无法归属才诚实放弃
                claimants = [a for a in srcs
                             if self._claims(scope.sources.get(a), node.name)]
                if len(claimants) == 1:
                    alias = claimants[0]
                elif node.name.lower() in self._using_cols(scope):
                    # USING 列双侧等值,SQL 语义取最左;两侧 schema 未知时同样成立
                    alias = claimants[0] if claimants else next(iter(srcs))
                else:
                    # 运行时唯一归属的对偶:列清单完备的来源均可否认该列,
                    # 若否认后只剩一个开放世界来源,列必出自它(SQL 对多来源
                    # 同名裸列报 ambiguous 错,能跑的查询恰有一个真主)
                    opens = [a for a in srcs
                             if not self._complete_cols(scope.sources.get(a))]
                    if not claimants and len(opens) == 1:
                        alias = opens[0]
                    else:
                        raise Unsup(
                            f"列 {node.name} 缺少限定名且作用域有多个来源,无法归属")
        src = scope.sources.get(alias)
        if src is None:
            hit = self._pivot_of(scope, alias)
            if hit is not None:
                return self._pivot_resolve(scope, *hit, node.name, depth, in_agg)
        if src is None and node.db and node.db in scope.sources:
            # 三段 a.b.c 且 a 是作用域源:b 是 a 的(结构体)列,c 是字段
            # (部分限定下 BigQuery 嵌套列的常见形态)
            base = exp.column(alias, table=node.db)
            expanded = self._expand_column(scope, base, depth, in_agg)
            return exp.Dot(this=expanded, expression=exp.to_identifier(node.name))
        if src is None:
            if node.db or node.catalog:
                # 直接两段/三段限定引用(未经别名):按物理表处理
                rel_txt = ".".join(p for p in (node.catalog, node.db, alias) if p)
                try:
                    rel = self.project.complete_rel(rel_txt)
                except ValueError as e:
                    raise Unsup(str(e)[:120])
                return self._resolve_rel(rel, node.name, scope, depth, in_agg)
            # 限定名不是任何源:单源作用域下按结构体列访问处理(限定名即基列名),
            # 基列是否真实由唯一源的常规解析裁决;多源无法归属,诚实放弃
            srcs = dict(scope.selected_sources or {})
            if len(srcs) == 1:
                base = exp.column(alias, table=next(iter(srcs)))
                expanded = self._expand_column(scope, base, depth, in_agg)
                return exp.Dot(this=expanded, expression=exp.to_identifier(node.name))
            raise Unsup(f"限定名 {alias} 在作用域中无来源(列 {node.name})")
        if isinstance(src, exp.Table):
            return self._resolve_rel(self._rel(src), node.name, scope, depth, in_agg)
        lat = getattr(src, "expression", None)
        _tfr = getattr(exp, "TableFromRows", ())
        flatten_like = isinstance(lat, (exp.Lateral,) + ((_tfr,) if _tfr else ()))
        if isinstance(lat, exp.Unnest) or flatten_like:
            # UNNEST / LATERAL FLATTEN / TABLE(FLATTEN(x)) 伪作用域:元素引用 =
            # 底层数组表达式(通道一血缘同此记账);数组在该伪作用域解析
            if flatten_like:
                if node.name.upper() != "VALUE":
                    raise Unsup(f"FLATTEN 伪列 {node.name} 非值列,不做组合声明")
                kw = next((k for k in lat.find_all(exp.Kwarg)), None)
                arr = kw.expression if kw is not None else next(
                    (c for c in lat.find_all(exp.Column)), None)
            else:
                arr = (lat.expressions or [None])[0] or lat.args.get("this")
            if arr is None:
                raise Unsup(f"UNNEST/FLATTEN 无数组参数(列 {node.name})")
            res_scope = src if (src.sources or {}) else scope
            expanded = self._expand(res_scope, arr.copy(), depth + 1, in_agg)
            # 匿名函数节点:各方言都渲染成 unnest(x)(exp.Unnest 在 snowflake
            # 生成器下会展开成 TABLE(FLATTEN(…)) 表构造,不适合公式展示)
            return exp.Anonymous(this="unnest", expressions=[expanded])
        body, grain = self._expand_scope_col(src, node.name, depth + 1)
        return self._consume(body, None, node.name, scope, in_agg, grain)

    # ---------------- PIVOT 展开 ----------------
    @staticmethod
    def _pivot_of(scope, alias: str | None):
        """作用域内挂在源 Subquery/Table 上的 Pivot:按 TableAlias 匹配(alias
        非 None),或取第一个(alias=None,供缺投影兜底)。返回 (Pivot, 输入 Scope)。"""
        for s in (scope.sources or {}).values():
            if isinstance(s, exp.Table) or not hasattr(s, "expression"):
                continue
            par = s.expression.parent
            for pv in (par.args.get("pivots") or []) if par is not None else []:
                al = pv.args.get("alias")
                nm = al.this.name if al is not None and al.this is not None else None
                if alias is None or nm == alias:
                    return pv, s
        return None

    def _pivot_resolve(self, scope, piv, input_scope, col: str,
                       depth: int, in_agg: bool) -> exp.Expression:
        """PIVOT 输出列的确定性改写:透视列 = 度量聚合参数包
        CASE WHEN <FOR 字段> = <值> THEN <参数> END;隐式分组 = 输入列 −
        度量引用列 − FOR 字段。id 列(不在输出清单)直通输入作用域。
        sqlglot 的 piv.args['columns'] 给出值主序输出列名,命名规则不自造。"""
        out_cols = [str(c.name) for c in piv.args.get("columns") or []]
        fields = piv.args.get("fields") or []
        measures = list(piv.expressions or [])
        low = [c.lower() for c in out_cols]
        if col.lower() not in low:
            body, grain = self._expand_scope_col(input_scope, col, depth + 1)
            return self._consume(body, None, col, scope, in_agg, grain)
        if len(fields) != 1 or not isinstance(fields[0], exp.In) or not measures:
            raise Unsup(f"PIVOT 结构非常规(多 FOR 字段/无度量),列 {col} 不展开")
        fld = fields[0]
        values = list(fld.expressions)
        i, nm = low.index(col.lower()), len(measures)
        if i // nm >= len(values):
            raise Unsup(f"PIVOT 输出列 {col} 超出值×度量矩阵,不展开")
        val, meas = values[i // nm], measures[i % nm]
        magg = (meas.this if isinstance(meas, exp.Alias) else meas).copy()
        wrapped = False
        for ag in magg.find_all(exp.AggFunc):
            arg = ag.this
            if arg is None:
                continue
            # COUNT(*) → COUNT(CASE WHEN f=v THEN 1 END):行计数按透视值过滤
            inner = exp.Literal.number(1) if isinstance(arg, exp.Star) else arg.copy()
            ag.set("this", exp.Case(ifs=[exp.If(
                this=exp.EQ(this=fld.this.copy(), expression=val.copy()), true=inner)]))
            wrapped = True
        if not wrapped:
            raise Unsup(f"PIVOT 度量无聚合参数可绑定(列 {col})")
        body = self._expand(scope, magg, depth + 1, in_agg)
        # 隐式分组 = 输入列 − 全部度量引用列 − FOR 字段(任一度量用到都不是分组键)
        used = {c.name.lower() for m in measures for c in m.find_all(exp.Column)}
        used.add(str(fld.this.name).lower())
        in_e = input_scope.expression
        grain = [p.alias_or_name for p in (in_e.expressions if isinstance(in_e, exp.Select) else [])
                 if p.alias_or_name and p.alias_or_name.lower() not in used]
        try:
            body.meta["def_grain"] = grain   # 钉在 body 上:def 化可能发生在后续消费位
        except Exception:
            pass
        return self._consume(body, None, col, scope, in_agg, grain)

    def _complete_cols(self, src) -> bool:
        """来源的列清单是否完备(可据以否认未列出的列):图内非降级模型的输出列
        完备;catalog 实测过的物理表完备。开放世界物理表(yml 部分声明/拓扑
        推断)、降级模型、表函数与含未展开星号的作用域都不完备——不完备者
        不能否认,只能作为运行时唯一归属的候选。"""
        if isinstance(src, exp.Table):
            try:
                rel = self._rel(src)
            except Unsup:
                return False
            up = self.rel_models.get(rel)
            if up and up in self.graph["models"]:
                return not (self.graph["models"][up].get("error"))
            return rel in self.project.catalog_rels
        if src is None or not hasattr(src, "expression"):
            return False
        e = src.expression
        if isinstance(e, (exp.Lateral, exp.Unnest) + _flatten_types()):
            return False    # 表函数伪作用域:输出列因函数而异,不据以否认
        return not any(
            isinstance(p, exp.Star)
            or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
            for s in e.find_all(exp.Select) for p in s.expressions)

    def _claims(self, src, col: str, _seen: set | None = None) -> bool:
        """来源是否担保列 col:Scope 看投影(星号按 except 递归其来源);模型表看
        图列;源表查 qualify schema;未知 schema 的物理表不担保(单源规则兜底)。"""
        low = col.lower()
        if isinstance(src, exp.Table):
            try:
                rel = self._rel(src)
            except Unsup:
                return False
            up = self.rel_models.get(rel)
            if up and up in self.graph["models"]:
                return low in {c.lower() for c in self.graph["models"][up].get("columns") or {}}
            node = self.project.schema
            for p in rel.split("."):
                node = node.get(p) if isinstance(node, dict) else None
            if isinstance(node, dict) and low in {c.lower() for c in node}:
                return True
            # 开放世界表(catalog 未实测)再问语料限定声明;封闭世界仍以
            # catalog 列集为准,不让语料边为不存在的列背书
            return (rel not in self.project.catalog_rels
                    and self._corpus_claim(rel, col))
        if src is None or not hasattr(src, "expression"):
            return False
        seen = _seen or set()
        if id(src) in seen:
            return False
        seen.add(id(src))
        e = src.expression
        if isinstance(e, (exp.Lateral, exp.Unnest) + _flatten_types()):
            # 表函数伪作用域只担保元素值列(FLATTEN/SPLIT_TO_TABLE 族的 VALUE;
            # 展开分支同此约定,非值伪列拒绝组合声明)
            return low == "value"
        if isinstance(e, exp.Unnest):
            return False
        if isinstance(e, _SETOP):
            us = src.union_scopes or []
            return bool(us) and self._claims(us[0], col, seen)
        if not isinstance(e, exp.Select):
            return False
        if any((p.alias_or_name or "").lower() == low for p in e.expressions):
            return True
        for p in e.expressions:
            if isinstance(p, exp.Star):
                exc = {c.name.lower() for c in p.args.get("except") or []}
                quals = list(dict(src.selected_sources or {}))
            elif isinstance(p, exp.Column) and isinstance(p.this, exp.Star):
                exc = {c.name.lower() for c in p.this.args.get("except") or []}
                quals = [p.table] if p.table else list(dict(src.selected_sources or {}))
            else:
                continue
            if low in exc:
                continue
            if any(self._claims(src.sources.get(q), col, seen) for q in quals):
                return True
        return False

    @staticmethod
    def _using_cols(scope) -> set:
        e = scope.expression
        out = set()
        if isinstance(e, exp.Select):
            for j in e.args.get("joins") or []:
                for c in j.args.get("using") or []:
                    out.add(str(getattr(c, "name", c)).lower())
        return out

    def _resolve_rel(self, rel: str, col: str, scope, depth: int, in_agg: bool) -> exp.Expression:
        up = self.rel_models.get(rel)
        if up and up in self.graph["models"]:
            m = self.graph["models"][up]
            if m.get("error") or col not in (m.get("columns") or {}):
                # 上游模型解析失败(或列未知):与 trace 同语义,血缘在其物化表
                # 截止,该引用按边界叶子记账——不展开也不报错
                self.leaves.add((rel, col))
                return exp.column(col, table=rel.split(".")[-1])
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
                # 定义处 grain 优先取钉在 body 上的(PIVOT 隐式分组经中间跳
                # 后 scope 级 grain 会丢),其次取调用方传入
                g = (getattr(body, "meta", None) or {}).get("def_grain") or grain
                return self._make_def(model_uid, col, body, g,
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

    def _branch_label(self, branch_scope) -> str:
        """UNION 分支标签:分支 FROM 的来源名(表→展示名,子查询→别名)。"""
        names = []
        for alias, pair in (branch_scope.selected_sources or {}).items():
            src = pair[-1] if isinstance(pair, tuple) else pair
            if isinstance(src, exp.Table):
                try:
                    rel = self._rel(src)
                except Unsup:
                    rel = table_key(src)
                up = self.rel_models.get(rel)
                names.append(self._disp(up) if up and up in self.graph["models"]
                             else (self.rel_sources.get(rel) or rel.split(".")[-1]))
            else:
                names.append(alias)
        return "+".join(names[:3]) or "branch"

    def _make_union_def(self, scope, col: str, branches: list, labels: list) -> exp.Column:
        """异构 UNION 的逐分支 def:branches 为 [(ast, grain)],与 labels 对位。
        单表达式不存在,但逐分支组合是确定性事实——kind=union 入 defs。
        键含 scope 身份:同模型内两个 union CTE 的同名列是不同定义,不得合并。"""
        owner = self.model_stack[-1] if self.model_stack else None
        key = ("union", id(scope), owner or "", col)
        d = self.def_by_key.get(key)
        if d is None:
            if len(self.defs) >= MAX_DEFS:
                raise Unsup("命名子表达式规模超限")
            disp = self._disp(owner) if owner else None
            name = col
            n = 2
            while any(x["name"] == name for x in self.defs):
                name = f"{disp}.{col}" if disp and n == 2 else f"{col}_{n}"
                n += 1
            br = [{"label": lb, "expr": _display(a)}
                  for lb, (a, _) in zip(labels, branches)]
            d = {"name": name, "model": disp, "model_uid": owner, "column": col,
                 "expr": f"UNION {len(br)} 分支(值=行所属分支的表达式)",
                 "ast": None, "grain": [], "kind": "union", "join_context": False,
                 "branches": br}
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
        out_defs = [{**{k: d[k] for k in ("name", "model", "column", "expr", "grain",
                                          "kind", "join_context")},
                     **({"branches": d["branches"]} if "branches" in d else {})}
                    for d in self.defs]
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
                    if d is not None and d.get("ast") is not None:   # union def 无单表达式形
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
    from fineprint.synth import formula_agg_check, verify_freetext

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
        # union def 的 expr 是中文摘要,闭包取其分支表达式(真实内容)参检
        closure = " ".join(
            [p["top"] or "" for p in ok_targets]
            + [" ".join(b["expr"] for b in d["branches"]) if d.get("branches") else d["expr"]
               for p in ok_targets for d in p["defs"]])
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
                   # dbt unique/relationships 测试 = 基数的声明性证据(dbt 实测)
                   "declared_unique": {comp._disp(mo): graph["models"][mo]["declared_unique"]
                                       for mo in t["models_visited"]
                                       if graph["models"].get(mo, {}).get("declared_unique")},
                   "declared_fk": {comp._disp(mo): graph["models"][mo]["declared_fk"]
                                   for mo in t["models_visited"]
                                   if graph["models"].get(mo, {}).get("declared_fk")},
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
    from fineprint.synth import norm_text
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


def formula_authority(facts: dict) -> str:
    """公式发布权威:规则可证用规则,规则不可证 LLM 兜底(赛马裁决后语义)。
    machine       组合器自证成立,发布公式=组合公式
    llm_fallback  组合器不可证(unsupported/多目标组合/标量子查询等),
                  发布公式=LLM 归并公式,按其全套互验置信裁决"""
    return "machine" if facts["formula"]["status"] == "proven" else "llm_fallback"


def publication_status(confidence: str, facts: dict, race: dict) -> str:
    """发布状态机(赛马已裁决:公式权威=组合器,LLM 退居解释与兜底。
    裁决依据:三语料 25412 列 proven 99.96% + demo 历史 disagree 全为 LLM 错):
    REVIEW_REQUIRED  round-trip 自检失败(机器两条实现互相矛盾)/ 过滤事实归因
                     不明 / 兜底路径上 LLM 未达高置信或被机器实锤——须人工
    VERIFIED         机器公式自证成立且 LLM 叙述过全部互验;或兜底路径 LLM 高
                     置信且无机器矛盾(此时卡上 authority=llm_fallback 带原因)
    TECHNICAL_ONLY   机器公式自证成立但 LLM 叙述未过互验(含 disagree:机器
                     事实照发,LLM 叙述待审)——技术事实可用,叙述待审
    (BLOCKED 保留:目标不可解析等硬失败当前直接报错,不落卡。)"""
    if (facts["key_filters"]["status"] == "ambiguous"
            or facts["formula"].get("rt_failed")):
        return "REVIEW_REQUIRED"
    narrative_ok = confidence == "high" and race.get("verdict") != "disagree"
    if facts["formula"]["status"] == "proven":
        return "VERIFIED" if narrative_ok else "TECHNICAL_ONLY"
    # 兜底路径:发布公式=LLM,须其自身互验高置信且无实锤矛盾才可发
    return "VERIFIED" if narrative_ok else "REVIEW_REQUIRED"
