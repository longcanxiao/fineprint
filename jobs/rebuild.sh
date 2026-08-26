#!/usr/bin/env bash
# MetricLens benchmark 数仓一键重建(原子发布):build 副本上完成 生成→dbt→验证→血缘,全部通过后原子替换。
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
DBT=.venv/bin/dbt
ML=.venv/bin/metriclens
PROJ="$(pwd)/warehouse/dbt_project"
FINAL_DB="$(pwd)/warehouse/metriclens.duckdb"
BUILD_DB="$(pwd)/warehouse/metriclens_build.duckdb"
FINAL_GRAPH="$PROJ/.metriclens/graph.json"
BUILD_GRAPH="$PROJ/.metriclens/graph_build.json"
export METRICLENS_DB="$BUILD_DB"
export METRICLENS_GRAPH="$BUILD_GRAPH"   # 血缘图同样在 build 位置产出,验证全过才切换
rm -f "$BUILD_DB" "$BUILD_GRAPH"

echo "==> [1/5] 生成 90 天四域 ODS 数据(build 库)"
$PY warehouse/simulator/generate.py

echo "==> [2/5] dbt build(14 模型 + 28 测试)+ docs generate(catalog)"
(cd "$PROJ" && ../../$DBT build --no-use-colors && ../../$DBT docs generate --no-use-colors -q)

echo "==> [3/5] 口径陷阱数据验证(14 道)"
$PY warehouse/evaluate/validate_traps.py

echo "==> [4/5] 指标手算对账(DWD 独立重算 vs APP 宽表)"
$PY warehouse/evaluate/handcheck_metrics.py

echo "==> [5/5] 血缘引擎:图重建 + manifest 骨架校验 + golden set + 重复扫描"
$ML graph --project "$PROJ"
$PY -m benchmark.manifest_check
$PY -m benchmark.eval_lineage
$PY -m benchmark.governance_scan_check

mv -f "$BUILD_DB" "$FINAL_DB"
mv -f "$BUILD_GRAPH" "$FINAL_GRAPH"
echo "✅ 全部验证通过,已原子发布 → $FINAL_DB + $FINAL_GRAPH(如 API 在运行,重启以刷新血缘缓存)"

echo "==> [后置] 口径漂移检测(基于正式图快照对比;记录不拦截)"
unset METRICLENS_DB METRICLENS_GRAPH   # 漂移快照必须基于刚发布的正式图
$ML drift --project "$PROJ"
