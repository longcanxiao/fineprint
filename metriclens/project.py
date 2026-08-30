#!/usr/bin/env python3
"""dbt 项目读取层:一切输入来自 dbt artifacts(manifest.json + catalog.json)。

不连接任何数据库——schema 取自 catalog.json,因此对 dbt 支持的所有仓库
(DuckDB/Snowflake/BigQuery/Postgres/Redshift/Databricks/…)一视同仁。
前置要求:`dbt compile`(产生编译 SQL)与 `dbt docs generate`(产生 catalog)。
"""
import json
import os
import re
from functools import cached_property
from pathlib import Path

from metriclens.config import read_internal_packages

ADAPTER_DIALECT = {
    "duckdb": "duckdb", "snowflake": "snowflake", "bigquery": "bigquery",
    "postgres": "postgres", "redshift": "redshift", "databricks": "databricks",
    "spark": "spark", "trino": "trino", "athena": "athena", "clickhouse": "clickhouse",
    "sqlserver": "tsql", "mysql": "mysql",
}

_AMBIG = object()   # _db_of 冲突哨兵:同 schema.table 出现在多个 database


def rel3(db, schema, table) -> str:
    """物理三段键 'database.schema.table':图内一切表反查的统一键形。
    缺段留空位(如 duckdb 无 database 的 '.main.orders')——manifest 侧与 SQL
    补全侧必须走同一构造,键才能互查。"""
    return f"{db or ''}.{schema or ''}.{table}"


class DbtProject:
    """已编译 dbt 项目的只读视图:模型、源表、schema、注释、方言。"""

    def __init__(self, project_dir: str | os.PathLike, target_dir: str | None = None,
                 internal_packages: tuple | None = None):
        self.project_dir = Path(project_dir).resolve()
        # 一方包名单:主项目自身 + 显式声明的内部共享包;其余包按数据源边界处理
        self.internal_packages = frozenset(
            internal_packages if internal_packages is not None
            else read_internal_packages(self.project_dir))
        self.target_dir = self._resolve_target(target_dir)
        mf = self.target_dir / "manifest.json"
        if not mf.exists():
            raise FileNotFoundError(
                f"未找到 {mf}\n请先在 dbt 项目里执行: dbt compile && dbt docs generate")
        self.manifest = json.loads(mf.read_text())
        cat = self.target_dir / "catalog.json"
        if not cat.exists():
            raise FileNotFoundError(
                f"未找到 {cat}(血缘解析需要列级 schema)\n请执行: dbt docs generate")
        self.catalog = json.loads(cat.read_text())

    def _resolve_target(self, target_dir: str | None) -> Path:
        """target 目录优先级:显式参数 → DBT_TARGET_PATH → dbt_project.yml 的 target-path → target。"""
        cand = target_dir or os.environ.get("DBT_TARGET_PATH")
        if not cand:
            f = self.project_dir / "dbt_project.yml"
            if f.exists():
                try:
                    import yaml
                    cand = (yaml.safe_load(f.read_text()) or {}).get("target-path")
                except Exception:
                    cand = None
        p = Path(cand) if cand else Path("target")
        return p if p.is_absolute() else self.project_dir / p

    # ---------------- 基本属性 ----------------
    @cached_property
    def adapter_type(self) -> str:
        return self.manifest["metadata"]["adapter_type"]

    @cached_property
    def root_package(self) -> str | None:
        """主项目包名:manifest 元数据优先(dbt ≥1.6),否则读 dbt_project.yml。
        两者都拿不到时返回 None → 无法定界,所有包按一方代码处理(绝不误折用户模型)。"""
        name = self.manifest.get("metadata", {}).get("project_name")
        if name:
            return name
        f = self.project_dir / "dbt_project.yml"
        if f.exists():
            try:
                import yaml
                return (yaml.safe_load(f.read_text()) or {}).get("name")
            except Exception:
                return None
        return None

    def _is_internal(self, pkg: str | None) -> bool:
        """一方代码判定:主项目、显式 internal_packages、以及缺 package_name 的节点
        (老 manifest)都算一方;其余第三方包按数据源边界处理,不解析其口径。"""
        root = self.root_package
        if root is None or pkg is None:
            return True
        return pkg == root or pkg in self.internal_packages

    @cached_property
    def dialect(self) -> str:
        d = ADAPTER_DIALECT.get(self.adapter_type)
        if d is None:
            raise ValueError(f"未适配的 dbt adapter: {self.adapter_type}"
                             f"(已支持: {sorted(ADAPTER_DIALECT)})")
        return d

    # ---------------- 模型与源表 ----------------
    @cached_property
    def models(self) -> dict:
        """{unique_id: {name, layer, schema, database, alias, package, sql, …}} —
        一方包的物化模型。主键 = dbt unique_id(逻辑身份,跨环境/跨配置稳定);
        物理落点(database.schema.alias)是属性,唯一性由 model_by_relation 防御复查。
        同名模型跨包合法共存,短名歧义在引用解析层显式报错。
        第三方包模型见 external_models(按数据源边界处理)。"""
        out = {}
        for uid, n in self.manifest["nodes"].items():
            if n.get("resource_type") != "model":
                continue
            if (n.get("config") or {}).get("materialized") == "ephemeral":
                continue
            if not self._is_internal(n.get("package_name")):
                continue          # 第三方包:不读取、不解析其 SQL
            cp = n.get("compiled_path")
            f = self.project_dir / cp if cp else None
            if f is None or not f.exists():
                raise FileNotFoundError(
                    f"模型 {n['name']} 缺少编译产物({cp});请先执行 dbt compile")
            fqn = n.get("fqn") or []
            layer = "/".join(fqn[1:-1]) or n.get("schema") or "default"
            out[uid] = {
                "name": n["name"],
                "layer": layer, "schema": n.get("schema"), "database": n.get("database"),
                "alias": n.get("alias") or n["name"], "package": n.get("package_name"),
                "sql": f.read_text(),
                "compiled_path": cp, "src_path": n.get("original_file_path"),
            }
        return out

    @cached_property
    def external_models(self) -> dict:
        """{'db.schema.alias': {name, alias, schema, database, package}} — 第三方包模型。

        与 ODS 同一约定:它们是别人维护的数据源,MetricLens 不解析其 SQL、注释与
        内部口径,血缘在其物化表处截止,治理扫描与卡片合成均不覆盖。需要看穿的
        内部共享包在 metriclens.yml 顶层 internal_packages 显式声明。"""
        out = {}
        for uid, n in self.manifest["nodes"].items():
            if n.get("resource_type") != "model":
                continue
            if (n.get("config") or {}).get("materialized") == "ephemeral":
                continue
            if self._is_internal(n.get("package_name")):
                continue
            alias = n.get("alias") or n["name"]
            out[rel3(n.get("database"), n.get("schema"), alias)] = {
                "name": n["name"], "alias": alias, "schema": n.get("schema"),
                "database": n.get("database"), "package": n.get("package_name")}
        return out

    @cached_property
    def exposures(self) -> dict:
        """{exposure name: {name, label, type, url, maturity, owner, models}} —
        dbt exposures = 项目外消费方声明(看板/报表/notebook/应用)。
        models 只收一方包物化模型 uid:消费方标注挂在指标出口模型上;
        依赖里的 source/第三方包(数据源边界)不入——那是上游,不是出口。"""
        out = {}
        for uid, e in (self.manifest.get("exposures") or {}).items():
            owner = e.get("owner") or {}
            name = e.get("name") or uid.split(".")[-1]
            out[name] = {
                "name": name, "label": e.get("label") or name,
                "type": e.get("type"), "url": e.get("url"),
                "maturity": e.get("maturity"),
                "owner": {k: owner[k] for k in ("name", "email") if owner.get(k)},
                "models": [d for d in (e.get("depends_on") or {}).get("nodes") or []
                           if d in self.models],
            }
        return out

    @cached_property
    def declared_tests(self) -> dict:
        """dbt schema 测试的基数声明(声明性证据,dbt 会按数据定期实测):
        {"unique": {被测节点 uid: [[列,...], ...]}, "fk": [{uid, column, to_uid, to_column}]}
        unique / dbt_utils.unique_combination_of_columns → 唯一键集;
        relationships → 外键声明(column 的值必存在于 to.field)。
        归属优先 attached_node,缺失时回落 depends_on(unique 类单依赖直取;
        relationships 以 to 的 ref/source 名剔除对端)。列名统一 lower 参与集合判定。"""
        uni: dict = {}
        fk: list = []
        for n in self.manifest["nodes"].values():
            if n.get("resource_type") != "test":
                continue
            tm = n.get("test_metadata") or {}
            kw = tm.get("kwargs") or {}
            deps = list((n.get("depends_on") or {}).get("nodes") or [])
            att = n.get("attached_node")
            if tm.get("name") == "unique":
                col = kw.get("column_name")
                owner = att or (deps[0] if len(deps) == 1 else None)
                if col and owner:
                    ks = [str(col).lower()]
                    uni.setdefault(owner, [])
                    if ks not in uni[owner]:
                        uni[owner].append(ks)
            elif tm.get("name") == "unique_combination_of_columns":
                cols = kw.get("combination_of_columns") or []
                owner = att or (deps[0] if len(deps) == 1 else None)
                if cols and owner:
                    ks = sorted(str(c).lower() for c in cols)
                    uni.setdefault(owner, [])
                    if ks not in uni[owner]:
                        uni[owner].append(ks)
            elif tm.get("name") == "relationships":
                col, field = kw.get("column_name"), kw.get("field")
                m = re.findall(r"'([^']+)'", str(kw.get("to") or ""))
                to_name = m[-1] if m else None
                to_uid = next((d for d in deps
                               if to_name and d.split(".")[-1] == to_name), None)
                owner = att or next((d for d in deps if d != to_uid), None)
                if col and field and owner and to_uid:
                    fk.append({"uid": owner, "column": str(col).lower(),
                               "to_uid": to_uid, "to_column": str(field).lower()})
        return {"unique": uni, "fk": fk}

    def rel_of_uid(self, uid: str) -> str | None:
        """manifest 节点/源 uid → 物理三段名(model 取 alias,source 取 identifier)。"""
        n = self.manifest["nodes"].get(uid) or (self.manifest.get("sources") or {}).get(uid)
        if not n:
            return None
        ident = n.get("alias") or n.get("identifier") or n.get("name")
        return rel3(n.get("database"), n.get("schema"), ident)

    @cached_property
    def declared_unique_rels(self) -> dict:
        """{物理三段名: [唯一键集,...]} — 唯一性声明折到物理表(模型与 source 都覆盖):
        血缘/治理据此对"join 到真实表"的伙伴做 N:1 判定。"""
        out: dict = {}
        for uid, sets in self.declared_tests["unique"].items():
            r = self.rel_of_uid(uid)
            if not r:
                continue
            out.setdefault(r, [])
            for s in sets:
                if s not in out[r]:
                    out[r].append(s)
        return out

    @cached_property
    def sources(self) -> dict:
        """{'db.schema.identifier': {schema, database, identifier, name}} — dbt source 表。
        物理三段键下,跨 schema、跨 database 的同名 identifier 都是不同数据,天然区分。"""
        out = {}
        for uid, s in self.manifest["sources"].items():
            ident = s.get("identifier") or s["name"]
            out[rel3(s.get("database"), s.get("schema"), ident)] = {
                "schema": s.get("schema"), "database": s.get("database"),
                "identifier": ident, "name": s["name"]}
        return out

    @staticmethod
    def _uniq(pairs, kind: str) -> dict:
        """物理三段名 → 逻辑身份的反查表必须单射:同一物理表由两个身份物化,
        dbt 解析期本应拒绝(AmbiguousAlias);artifacts 是外部输入,此处防御复查。"""
        out = {}
        for rel, name in pairs:
            if rel in out and out[rel] != name:
                raise ValueError(
                    f"{kind}物理落点冲突: {rel} 同时由 {out[rel]} 与 {name} 物化"
                    f"(dbt 正常编译不会产生;manifest/catalog 可能异常,请重新 dbt compile)")
            out[rel] = name
        return out

    @cached_property
    def model_by_relation(self) -> dict:
        """'db.schema.alias' → unique_id(血缘 upstream 表 → 模型的反查)。"""
        return self._uniq(((rel3(m["database"], m["schema"], m["alias"]), uid)
                           for uid, m in self.models.items()), "模型")

    @cached_property
    def source_by_relation(self) -> dict:
        """'db.schema.identifier' → source_identifier(裸名,供 trace 展示与 LLM 互验)。
        第三方包模型的物化表并入此表——对血缘而言它们就是数据源。"""
        pairs = [(rel, s["identifier"]) for rel, s in self.sources.items()]
        pairs += [(rel, e["alias"]) for rel, e in self.external_models.items()]
        return self._uniq(pairs, "源表")

    # ---------------- 物理键补全 ----------------
    @cached_property
    def _db_of(self) -> dict:
        """'schema.table' → database(catalog 全体物理表):SQL 里常写两段名,
        图键统一三段,建图期据此补全;同 schema.table 现于多个 database 时登记
        冲突哨兵,补全时显式报错(与 sqlglot qualify 的 Ambiguous mapping 同界)。"""
        m: dict = {}
        for coll in (self.catalog.get("nodes", {}), self.catalog.get("sources", {})):
            for entry in coll.values():
                md = entry["metadata"]
                key = f'{md["schema"]}.{md["name"]}'
                db = md.get("database") or ""
                if key in m and m[key] != db:
                    m[key] = _AMBIG
                else:
                    m.setdefault(key, db)
        return m

    def complete_rel(self, rel: str) -> str:
        """表引用 → 三段物理键。三段原样;两段查 catalog 补 database;
        裸名无 schema 无从补全,原样返回(反查不中即按未知外部源处理)。"""
        parts = rel.split(".")
        if len(parts) >= 3 or len(parts) == 1:
            return rel
        db = self._db_of.get(rel)
        if db is _AMBIG:
            raise ValueError(
                f"表引用 {rel} 在多个 database 中同名,两段名无法定位;请在 SQL 中写全三段名")
        return f"{db}.{rel}" if db is not None else f".{rel}"

    # ---------------- schema(供 sqlglot qualify)----------------
    @cached_property
    def schema(self) -> dict:
        """{database: {schema: {table: {column: type}}}},来自 catalog.json;
        catalog 缺席的源表回落到 manifest sources 的 yml 声明列(一方声明,
        docs 站产物常见 catalog 不含 sources——Snowplow 探针实证),绝不覆盖
        catalog 已有条目。"""
        schema: dict = {}
        for coll in (self.catalog.get("nodes", {}), self.catalog.get("sources", {})):
            for entry in coll.values():
                md = entry["metadata"]
                db, sch, tbl = md.get("database") or "", md["schema"], md["name"]
                cols = {c["name"]: c["type"] for c in entry["columns"].values()}
                if not cols:
                    continue
                schema.setdefault(db, {}).setdefault(sch, {})[tbl] = cols
        for s in self.manifest.get("sources", {}).values():
            db, sch = s.get("database") or "", s.get("schema")
            tbl = s.get("identifier") or s.get("name")
            cols = {c["name"]: (c.get("data_type") or "unknown")
                    for c in (s.get("columns") or {}).values() if c.get("name")}
            if cols and tbl and sch:
                schema.setdefault(db, {}).setdefault(sch, {}).setdefault(tbl, cols)
        return schema

    # ---------------- 业务注释(供口径合成)----------------
    @cached_property
    def column_docs(self) -> dict:
        """{table_or_model_name: {column: description}},来自 manifest 的 schema.yml 文档。
        裸名键会被同名对象互相覆盖,只作最后回退;每个对象同时登记不折叠的全名键:
        模型 'package:name'(与展示名规则一致),源表 'schema.identifier' 与
        'db.schema.identifier'——消费方按全名 → 裸名的顺序查询。
        只收一方包的注释:第三方包的 description 是包作者文本,不得进 LLM 上下文。"""
        docs: dict = {}

        def put(keys, cols):
            for col, meta in (cols or {}).items():
                if meta.get("description"):
                    for k in keys:
                        docs.setdefault(k, {})[col] = meta["description"]

        for n in self.manifest.get("nodes", {}).values():
            if not self._is_internal(n.get("package_name")):
                continue
            keys = [n.get("name")]
            if n.get("resource_type") == "model" and n.get("package_name"):
                keys.append(f'{n["package_name"]}:{n["name"]}')
            put(keys, n.get("columns"))
        for n in self.manifest.get("sources", {}).values():
            if not self._is_internal(n.get("package_name")):
                continue
            tbl = n.get("identifier") or n.get("name")
            keys = [tbl]
            if n.get("schema"):
                keys.append(f'{n["schema"]}.{tbl}')
                keys.append(rel3(n.get("database"), n["schema"], tbl))
            put(keys, n.get("columns"))
        return docs

    # ---------------- 工作目录 ----------------
    @cached_property
    def workspace(self) -> Path:
        """MetricLens 的全部产物都放在被分析项目的 .metriclens/ 下。"""
        ws = self.project_dir / ".metriclens"
        ws.mkdir(exist_ok=True)
        return ws

    def graph_path(self) -> Path:
        return Path(os.environ.get("METRICLENS_GRAPH") or self.workspace / "graph.json")
