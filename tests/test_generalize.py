# -*- coding: utf-8 -*-
"""通用化缺陷回归:CTE 行集条件、COUNT(*) 表级回退、relation 碰撞、metric key 校验。

外部复审探针固化:jaffle_shop/任意 dbt 项目上会真实出现的结构,不依赖 benchmark 数仓。
"""
import json
import sys
from pathlib import Path

import pytest
import sqlglot

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from metriclens.lineage import build_graph, row_scope_closure  # noqa: E402
from metriclens.project import DbtProject  # noqa: E402
from metriclens.trace import trace  # noqa: E402


def _node(name, sql_rel, schema="main"):
    return {"resource_type": "model", "name": name, "schema": schema, "alias": name,
            "fqn": ["p", name], "compiled_path": sql_rel,
            "original_file_path": f"models/{name}.sql", "config": {"materialized": "table"}}


def _cat(schema, name, cols):
    return {"metadata": {"schema": schema, "name": name},
            "columns": {c: {"name": c, "type": t} for c, t in cols.items()}}


def make_project(tmp_path, nodes, catalog_nodes, sqls, sources=None,
                 project_name=None, internal_packages=None, adapter="duckdb",
                 exposures=None):
    proj = tmp_path / "proj"
    (proj / "target").mkdir(parents=True)
    for rel, text in sqls.items():
        f = proj / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    meta = {"adapter_type": adapter}
    if project_name:
        meta["project_name"] = project_name   # 缺省不写:root 未知 → 全部按一方包
    (proj / "target" / "manifest.json").write_text(json.dumps({
        "metadata": meta, "nodes": nodes, "sources": sources or {},
        "exposures": exposures or {}}))
    (proj / "target" / "catalog.json").write_text(json.dumps(
        {"nodes": catalog_nodes, "sources": {}}))
    return DbtProject(proj, internal_packages=internal_packages)


@pytest.fixture()
def rowset_project(tmp_path):
    """seed → filtered(where active=1) → metric(count(*) 经 CTE):count(*) 无列引用。"""
    return make_project(
        tmp_path,
        nodes={"model.p.filtered": _node("filtered", "compiled/filtered.sql"),
               "model.p.metric": _node("metric", "compiled/metric.sql")},
        catalog_nodes={
            "seed.p.raw_events": _cat("main", "raw_events",
                                      {"id": "INTEGER", "active": "INTEGER", "amount": "INTEGER"}),
            "model.p.filtered": _cat("main", "filtered",
                                     {"id": "INTEGER", "active": "INTEGER", "amount": "INTEGER"}),
            "model.p.metric": _cat("main", "metric", {"n": "BIGINT", "amt": "HUGEINT"})},
        sqls={"compiled/filtered.sql": "select * from main.raw_events where active = 1",
              "compiled/metric.sql": ("with base as (select * from main.filtered)\n"
                                      "select count(*) as n, sum(amount) as amt from base")})


class TestRowScopeClosure:
    """sqlglot 30 起 Select 的 from 参数 key 是 from_;FROM 挂接的 CTE 必须进行集闭包。"""

    def test_from_linked_cte_in_closure(self):
        ast = sqlglot.parse_one(
            "with filtered as (select * from raw where active = 1) select count(*) as n from filtered",
            read="duckdb")
        assert "filtered" in row_scope_closure(ast)

    def test_left_join_cte_excluded(self):
        ast = sqlglot.parse_one(
            "with a as (select * from t1), b as (select * from t2 where x = 1) "
            "select count(*) as n from a left join b on a.id = b.id", read="duckdb")
        c = row_scope_closure(ast)
        assert "a" in c and "b" not in c


class TestCountStarRowset:
    """COUNT(*) 无列引用 → 列级血缘断链,必须以表级行集上游兜底,过滤条件不得丢失。"""

    def test_graph_falls_back_to_rowset_tables(self, rowset_project):
        g = build_graph(rowset_project)
        ups = g["models"]["model.p.metric"]["columns"]["n"]["upstreams"]
        assert ups == [{"table": ".main.filtered", "column": "*"}]     # 物理三段键,db 缺位留空
        assert g["models"]["model.p.filtered"]["row_set_tables"] == [".main.raw_events"]

    def test_trace_keeps_upstream_filter(self, rowset_project):
        g = build_graph(rowset_project)
        t = trace(g, "metric", "n")                                    # 短名经 resolve 归位 uid
        assert "model.p.filtered" in t["models_visited"]
        assert any(c["sql"] == "active = 1" for c in t["conditions"])
        assert {"table": "raw_events", "schema": "main", "database": "", "column": "*"} in t["sources"]

    def test_value_path_unaffected(self, rowset_project):
        g = build_graph(rowset_project)
        t = trace(g, "metric", "amt")
        assert {"table": "raw_events", "schema": "main", "database": "",
                "column": "amount"} in t["sources"]
        assert any(c["sql"] == "active = 1" for c in t["conditions"])

    def test_single_model_cte_condition_row_level(self, rowset_project):
        g = build_graph(rowset_project)
        conds = {c["sql"]: c for c in g["models"]["model.p.filtered"]["conditions"]}
        assert conds["active = 1"]["row_level"] is True


class TestRelationCollision:
    """同一物理落点由两个身份物化 = artifacts 异常,防御性拒绝;
    跨 database 同 schema.table 在物理三段键下是不同表,0.7 起合法。"""

    def test_model_alias_collision_raises(self, tmp_path):
        n1 = _node("m1", "compiled/m1.sql")
        n2 = _node("m2", "compiled/m2.sql")
        n2["alias"] = "m1"                      # 两个模型物化到同一 db.schema.alias
        p = make_project(tmp_path,
                         nodes={"model.p.m1": n1, "model.p.m2": n2},
                         catalog_nodes={"model.p.m1": _cat("main", "m1", {"x": "INT"})},
                         sqls={"compiled/m1.sql": "select 1 as x",
                               "compiled/m2.sql": "select 2 as x"})
        with pytest.raises(ValueError, match="物理落点冲突"):
            _ = p.model_by_relation

    def test_cross_database_same_identifier_now_legal(self, tmp_path):
        p = make_project(
            tmp_path,
            nodes={"model.p.m1": _node("m1", "compiled/m1.sql")},
            catalog_nodes={"model.p.m1": _cat("main", "m1", {"x": "INT"})},
            sqls={"compiled/m1.sql": "select 1 as x"},
            sources={"source.p.a.orders": {"identifier": "orders", "name": "orders",
                                           "schema": "shared", "database": "db1"},
                     "source.p.b.orders": {"identifier": "orders", "name": "orders",
                                           "schema": "shared", "database": "db2"}})
        assert set(p.sources) == {"db1.shared.orders", "db2.shared.orders"}
        assert p.source_by_relation == {"db1.shared.orders": "orders",
                                        "db2.shared.orders": "orders"}


class TestMetricKeyValidation:
    """metric key 直接拼接文件路径:逃逸/重复/保留名一律拒绝。"""

    def _load(self, tmp_path, keys):
        from metriclens.config import MLConfig
        items = "\n".join(f"  - key: {json.dumps(k)}\n    target: m.c" for k in keys)
        (tmp_path / "metriclens.yml").write_text(f"language: zh\nmetrics:\n{items}\n")
        return MLConfig.load(tmp_path)

    def test_path_escape_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="非法"):
            self._load(tmp_path, ["../../outside"])

    def test_absolute_path_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="非法"):
            self._load(tmp_path, ["/etc/evil"])

    def test_duplicate_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="重复"):
            self._load(tmp_path, ["same", "same"])

    def test_reserved_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="保留名"):
            self._load(tmp_path, ["index"])

    def test_normal_keys_pass(self, tmp_path):
        cfg = self._load(tmp_path, ["gmv", "refund_rate_14d", "a.b-c"])
        assert [m.key for m in cfg.metrics] == ["gmv", "refund_rate_14d", "a.b-c"]


class TestTargetPathResolution:
    def test_env_and_yaml_resolution(self, tmp_path, monkeypatch):
        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "dbt_project.yml").write_text("name: p\ntarget-path: custom_target\n")
        monkeypatch.delenv("DBT_TARGET_PATH", raising=False)
        with pytest.raises(FileNotFoundError, match="custom_target"):
            DbtProject(proj)                     # yml 的 target-path 参与解析
        monkeypatch.setenv("DBT_TARGET_PATH", "env_target")
        with pytest.raises(FileNotFoundError, match="env_target"):
            DbtProject(proj)                     # 环境变量优先于 yml


class TestDriftGate:
    """strict 门禁语义:high 漂移时基线与事件日志均不推进,失败可复现。"""

    def _setup(self, rowset_project):
        from metriclens.config import MetricDef, MLConfig
        from metriclens.drift import run_check
        cfg = MLConfig(metrics=[MetricDef(key="amt", title="amt", target="metric.amt")])
        g = build_graph(rowset_project)
        run_check(rowset_project, cfg, g)        # 建基线
        import copy
        g2 = copy.deepcopy(g)
        c = g2["models"]["model.p.filtered"]["conditions"][0]
        c.update(sql="active = 2", norm="active = 2", fp="fp_changed_xx")
        return cfg, g2, run_check

    def test_strict_high_blocks_commit(self, rowset_project):
        cfg, g2, run_check = self._setup(rowset_project)
        ws = rowset_project.workspace
        ev = run_check(rowset_project, cfg, g2, block_high=True)
        assert any(e["severity"] == "high" for e in ev)
        assert len(list((ws / "snapshots").glob("*.json"))) == 1   # 基线未推进
        assert not (ws / "drift_log.json").exists()                # 日志未写入
        ev2 = run_check(rowset_project, cfg, g2, block_high=True)  # 第二次仍失败
        assert any(e["severity"] == "high" for e in ev2)

    def test_non_strict_commits(self, rowset_project):
        cfg, g2, run_check = self._setup(rowset_project)
        ws = rowset_project.workspace
        run_check(rowset_project, cfg, g2)
        assert len(list((ws / "snapshots").glob("*.json"))) == 2
        assert (ws / "drift_log.json").exists()


class TestConfigDrift:
    """target 改指向与 query_filter 变化是口径实质变化;老快照缺键不误报。"""

    _base = {"target": "m.c", "query_filter": None, "sources": [], "conditions": {},
             "semantics": [], "exprs": {}}

    def test_target_changed_high(self):
        from metriclens.drift import diff_metric
        new = {**self._base, "target": "m.d"}
        assert [(e["kind"], e["severity"]) for e in diff_metric("k", self._base, new)] \
            == [("target_changed", "high")]

    def test_query_filter_changed_high(self):
        from metriclens.drift import diff_metric
        new = {**self._base, "query_filter": "channel = 'live'"}
        assert [(e["kind"], e["severity"]) for e in diff_metric("k", self._base, new)] \
            == [("query_filter_changed", "high")]

    def test_legacy_snapshot_without_key_silent(self):
        from metriclens.drift import diff_metric
        old = {k: v for k, v in self._base.items() if k != "query_filter"}
        new = {**self._base, "query_filter": "x = 1"}
        assert diff_metric("k", old, new) == []


class TestGrainAndAggSignature:
    """治理粒度签名:grain 沿 FROM 主链取第一个聚合层;空签名不可比(缺证不下结论)。"""

    def test_grain_from_cte_chain(self):
        from metriclens.lineage import output_grain
        ast = sqlglot.parse_one(
            "with agg as (select dt, sum(x) as gmv from t group by dt) "
            "select a.dt, a.gmv, b.y from agg a join other b on a.dt = b.dt", read="duckdb")
        assert output_grain(ast) == ["dt"]

    def test_grain_top_level_group(self):
        from metriclens.lineage import output_grain
        ast = sqlglot.parse_one("select dt, ch, sum(x) as v from t group by 1, 2", read="duckdb")
        assert output_grain(ast) == ["ch", "dt"]

    def test_grain_detail_empty(self):
        from metriclens.lineage import output_grain
        ast = sqlglot.parse_one("select id, x from t where x > 0", read="duckdb")
        assert output_grain(ast) == []

    def test_agg_signature_distinct_marked(self):
        from metriclens.fingerprint import agg_signature
        t = {"expr_chain": [{"expr": "COUNT(DISTINCT user_id)"}, {"expr": "MIN(dt)"}]}
        assert agg_signature(t) == ("count:distinct", "min")


class TestLLMErrorClassification:
    """4xx(非 408/429)不可重试:一次即失败,不烧 8 轮退避。"""

    def _fake_response(self, status, body="denied"):
        class R:
            status_code = status
            text = body
            headers = {}
        return R()

    def test_401_fails_fast(self, monkeypatch):
        from metriclens import llm
        calls = {"n": 0}

        def fake_post(*a, **k):
            calls["n"] += 1
            return self._fake_response(401)
        monkeypatch.setattr(llm.requests, "post", fake_post)
        cfg = {"base_url": "http://x", "api_key": "k"}
        with pytest.raises(llm.FatalLLMError, match="401"):
            llm._request(cfg, "m", "s", "u", 100, None, None)
        assert calls["n"] == 1

    def test_429_honors_retry_after(self, monkeypatch):
        from metriclens import llm
        r = self._fake_response(429)
        r.headers = {"Retry-After": "3"}
        seen = []
        monkeypatch.setattr(llm.time, "sleep", seen.append)
        monkeypatch.setattr(llm.requests, "post", lambda *a, **k: r)
        cfg = {"base_url": "http://x", "api_key": "k"}
        with pytest.raises(RuntimeError, match="重试 8 次"):
            llm._request(cfg, "m", "s", "u", 100, None, None)
        assert seen and all(s == 3.0 for s in seen)


class TestParseFailureBoundary:
    """真实项目探针固化(Snowplow):单模型解析/qualify 失败不得炸整图——
    该模型降级为边界节点,血缘与组合器都在其物化表处截止(与第三方包同语义)。"""

    def _proj(self, tmp_path):
        return make_project(
            tmp_path,
            nodes={"model.p.broken": _node("broken", "compiled/broken.sql"),
                   "model.p.consumer": _node("consumer", "compiled/consumer.sql")},
            catalog_nodes={
                "model.p.broken": _cat("main", "broken", {"v": "INTEGER"}),
                "model.p.consumer": _cat("main", "consumer", {"total": "INTEGER"})},
            sqls={"compiled/broken.sql": "THIS IS ((( not sql AT ALL",
                  "compiled/consumer.sql": "select sum(v) as total from main.broken"})

    def test_graph_survives_and_marks_boundary(self, tmp_path):
        g = build_graph(self._proj(tmp_path))
        assert g["models"]["model.p.broken"]["error"].startswith("parse:")
        assert g["models"]["model.p.broken"]["columns"] == {}
        assert "total" in g["models"]["model.p.consumer"]["columns"]

    def test_trace_stops_at_boundary_table(self, tmp_path):
        g = build_graph(self._proj(tmp_path))
        t = trace(g, "consumer", "total")
        assert [(s["table"], s["column"]) for s in t["sources"]] == [("broken", "v")]

    def test_composer_leafs_boundary(self, tmp_path):
        from metriclens.render import _Composer
        proj = self._proj(tmp_path)
        g = build_graph(proj)
        c = _Composer(proj, g).compose_target("model.p.consumer", "total")
        assert c["status"] == "proven"
        assert (".main.broken", "v") in set(map(tuple, c["leaf_pairs"]))

    def test_target_itself_broken_errors_clearly(self, tmp_path):
        g = build_graph(self._proj(tmp_path))
        with pytest.raises(KeyError, match="解析失败"):
            trace(g, "broken", "v")


class TestManifestSourceSchemaFallback:
    """docs 站产物常见:catalog 无 sources。manifest 里一方 yml 声明的源表列
    回落进 qualify schema(catalog 优先,只补缺席表),多表 join 的裸列得以定位。"""

    def test_declared_columns_enable_qualify(self, tmp_path):
        proj = make_project(
            tmp_path,
            nodes={"model.p.m": _node("m", "compiled/m.sql")},
            catalog_nodes={"model.p.m": _cat("main", "m", {"app_id": "TEXT", "n": "INTEGER"})},
            sqls={"compiled/m.sql": (
                "select app_id, count(*) as n from main.raw_events e "
                "join main.raw_users u on e.uid = u.uid group by 1")},
            sources={"source.p.raw.raw_events": {
                        "resource_type": "source", "name": "raw_events", "schema": "main",
                        "database": "", "identifier": "raw_events",
                        "columns": {"app_id": {"name": "app_id"}, "uid": {"name": "uid"}}},
                     "source.p.raw.raw_users": {
                        "resource_type": "source", "name": "raw_users", "schema": "main",
                        "database": "", "identifier": "raw_users",
                        "columns": {"uid": {"name": "uid"}}}})
        g = build_graph(proj)
        m = g["models"]["model.p.m"]
        assert "error" not in m
        entry = m["columns"]["app_id"]
        assert not entry.get("error")
        assert {(u["table"], u["column"]) for u in entry["upstreams"]} == {(".main.raw_events", "app_id")}
