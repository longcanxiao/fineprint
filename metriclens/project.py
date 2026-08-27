#!/usr/bin/env python3
"""dbt 项目读取层:一切输入来自 dbt artifacts(manifest.json + catalog.json)。

不连接任何数据库——schema 取自 catalog.json,因此对 dbt 支持的所有仓库
(DuckDB/Snowflake/BigQuery/Postgres/Redshift/Databricks/…)一视同仁。
前置要求:`dbt compile`(产生编译 SQL)与 `dbt docs generate`(产生 catalog)。
"""
import json
import os
from functools import cached_property
from pathlib import Path

ADAPTER_DIALECT = {
    "duckdb": "duckdb", "snowflake": "snowflake", "bigquery": "bigquery",
    "postgres": "postgres", "redshift": "redshift", "databricks": "databricks",
    "spark": "spark", "trino": "trino", "athena": "athena", "clickhouse": "clickhouse",
    "sqlserver": "tsql", "mysql": "mysql",
}


class DbtProject:
    """已编译 dbt 项目的只读视图:模型、源表、schema、注释、方言。"""

    def __init__(self, project_dir: str | os.PathLike, target_dir: str | None = None):
        self.project_dir = Path(project_dir).resolve()
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
    def dialect(self) -> str:
        d = ADAPTER_DIALECT.get(self.adapter_type)
        if d is None:
            raise ValueError(f"未适配的 dbt adapter: {self.adapter_type}"
                             f"(已支持: {sorted(ADAPTER_DIALECT)})")
        return d

    # ---------------- 模型与源表 ----------------
    @cached_property
    def models(self) -> dict:
        """{model_name: {layer, schema, alias, sql, compiled_path, src_path}} — 仅物化模型。"""
        out = {}
        for uid, n in self.manifest["nodes"].items():
            if n.get("resource_type") != "model":
                continue
            if (n.get("config") or {}).get("materialized") == "ephemeral":
                continue
            cp = n.get("compiled_path")
            f = self.project_dir / cp if cp else None
            if f is None or not f.exists():
                raise FileNotFoundError(
                    f"模型 {n['name']} 缺少编译产物({cp});请先执行 dbt compile")
            fqn = n.get("fqn") or []
            layer = "/".join(fqn[1:-1]) or n.get("schema") or "default"
            if n["name"] in out:
                raise ValueError(
                    f"模型名折叠冲突: {n['name']} 同时定义于包 "
                    f"{out[n['name']]['package']} 与 {n.get('package_name')}"
                    f"(dbt 允许跨包同名;MetricLens 暂以模型名为主键,unique_id 支持在路线图)")
            out[n["name"]] = {
                "layer": layer, "schema": n.get("schema"), "database": n.get("database"),
                "alias": n.get("alias") or n["name"], "package": n.get("package_name"),
                "sql": f.read_text(),
                "compiled_path": cp, "src_path": n.get("original_file_path"),
            }
        return out

    @cached_property
    def sources(self) -> dict:
        """{'schema.identifier': {schema, database, identifier, name}} — dbt source 表。
        不同 schema 的同名 identifier 是合法用法(erp.orders / crm.orders),各自独立;
        只有 schema.identifier 全同而 database 不同才是当前身份体系无法表达的折叠。"""
        out = {}
        for uid, s in self.manifest["sources"].items():
            ident = s.get("identifier") or s["name"]
            key = f'{s.get("schema")}.{ident}'
            rec = {"schema": s.get("schema"), "database": s.get("database"),
                   "identifier": ident, "name": s["name"]}
            if key in out and out[key]["database"] != rec["database"]:
                raise ValueError(
                    f"源表标识折叠冲突: {key} 同时声明于 database "
                    f"{out[key]['database']} 与 {rec['database']}(多 database 同名源表暂不支持)")
            out[key] = rec
        return out

    @staticmethod
    def _uniq(pairs, kind: str) -> dict:
        """反查表要求 'schema.table' 全局唯一;跨 database 同名折叠会产生错误血缘,显式拒绝。"""
        out = {}
        for rel, name in pairs:
            if rel in out and out[rel] != name:
                raise ValueError(
                    f"{kind}关系折叠冲突: {rel} 同时对应 {out[rel]} 与 {name}"
                    f"(多 database 同 schema.table 项目暂不支持,请重命名或拆分 schema)")
            out[rel] = name
        return out

    @cached_property
    def model_by_relation(self) -> dict:
        """'schema.alias' → model_name(血缘 upstream 表 → 模型的反查)。"""
        return self._uniq(((f'{m["schema"]}.{m["alias"]}', name)
                           for name, m in self.models.items()), "模型")

    @cached_property
    def source_by_relation(self) -> dict:
        """'schema.identifier' → source_identifier(裸名,供 trace 展示与 LLM 互验)。"""
        return self._uniq(((f'{s["schema"]}.{s["identifier"]}', s["identifier"])
                           for s in self.sources.values()), "源表")

    # ---------------- schema(供 sqlglot qualify)----------------
    @cached_property
    def schema(self) -> dict:
        """{database: {schema: {table: {column: type}}}},来自 catalog.json。"""
        schema: dict = {}
        for coll in (self.catalog.get("nodes", {}), self.catalog.get("sources", {})):
            for entry in coll.values():
                md = entry["metadata"]
                db, sch, tbl = md.get("database") or "", md["schema"], md["name"]
                cols = {c["name"]: c["type"] for c in entry["columns"].values()}
                if not cols:
                    continue
                schema.setdefault(db, {}).setdefault(sch, {})[tbl] = cols
        return schema

    # ---------------- 业务注释(供口径合成)----------------
    @cached_property
    def column_docs(self) -> dict:
        """{table_or_model_name: {column: description}},来自 manifest 的 schema.yml 文档。"""
        docs: dict = {}
        for coll in (self.manifest.get("nodes", {}), self.manifest.get("sources", {})):
            for n in coll.values():
                tbl = n.get("identifier") or n.get("name")
                for col, meta in (n.get("columns") or {}).items():
                    if meta.get("description"):
                        docs.setdefault(tbl, {})[col] = meta["description"]
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
