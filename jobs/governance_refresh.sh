#!/usr/bin/env bash
# 治理报告刷新:指纹扫描 + B 档 LLM 语义仲裁(有 LLM 调用,与数据重建解耦)。
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/fineprint govern --project warehouse/dbt_project
