#!/usr/bin/env bash
# 口径批次刷新:双通道合成(LLM,带缓存)整批原子发布 + 陷阱揭示评测。
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/fineprint synth --project warehouse/dbt_project "$@"
.venv/bin/python -m benchmark.eval_caliber
