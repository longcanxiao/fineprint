#!/usr/bin/env bash
# 口径卡全量刷新(涉及 LLM 调用,与确定性重建 rebuild.sh 分离)
# 前置:jobs/rebuild.sh 已跑通(血缘图为最新);DeepSeek 配置见 caliber/llm.py 的 CALIBER_ENV_FILE
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "==> [1/2] 口径合成:通道二逐跳解析 + 双通道互验 + 业务口径生成(15 张卡)"
$PY -m caliber.pipeline

echo "==> [2/2] 陷阱揭示评测(14 道,验收线 ≥12)"
$PY -m caliber.eval_caliber
# 注:pipeline 整批写入 store/runs/<run_id>/,任一指标失败即非零退出且不切换 active_run 指针,
# API 始终只读上一个完整批次——失败批次残留在 runs/ 目录内供排查
