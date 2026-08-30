#!/usr/bin/env bash
# 公开发行版构建:在暂存树上摘除治理组件与 exposures 功能后打包。
#
#   ./scripts/release_public.sh          # 产物落 dist/
#
# 摘除内容(仓库本体一行不动):
#   - 治理组件:metriclens/governance.py + metriclens/arbitrate.py 整文件删除
#     (synth 对 governance 是可选导入,缺席时卡片治理提示区为空;
#      cli govern 缺席时给出友好提示;仲裁提示词与 arbitrate 同居一并缺席)
#   - exposures 功能:git revert exposures 提交(EXPOSURES_COMMIT)
# sdist 本就只含 metriclens/ + README.pypi.md + LICENSE(见 pyproject sdist 配置),
# demo 数仓/看板/benchmark/设计文档/测试不随包发布。
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
EXPOSURES_COMMIT=e504bd8
STAGE=$(mktemp -d /tmp/fineprint_release.XXXXXX)
trap 'git -C "$REPO" worktree remove --force "$STAGE" 2>/dev/null || true; rm -rf "$STAGE"' EXIT

cd "$REPO"
[ -z "$(git status --porcelain)" ] || { echo "工作区不干净,先提交或暂存"; exit 1; }

git worktree add --detach "$STAGE" HEAD >/dev/null
cd "$STAGE"
git revert --no-commit "$EXPOSURES_COMMIT" || { echo "revert 冲突:exposures 摘除需人工处理"; exit 1; }
rm metriclens/governance.py metriclens/arbitrate.py

# 摘除后自检 1:被删符号不得残留于发行包
! grep -rn "target_exposures\|exposures_by_model\|annotate_exposures" metriclens/ \
  || { echo "exposures 残留于 metriclens/"; exit 1; }
! grep -rn "^from metriclens.governance\|^from metriclens.arbitrate" metriclens/ \
  || { echo "治理硬依赖残留"; exit 1; }

# 摘除后自检 2:跑不依赖治理/exposures 的测试子集(revert 已删 test_exposures)
PY="$REPO/.venv/bin/python"
IGNORES=$(grep -rl "metriclens.governance\|metriclens.arbitrate" tests/ | sed 's/^/--ignore=/' | tr '\n' ' ')
"$PY" -m pytest tests/ $IGNORES -q || { echo "摘除后测试子集失败"; exit 1; }

rm -rf "$REPO/dist"
uv build --out-dir "$REPO/dist" >/dev/null

# 产物自检:治理/exposures/密钥都不得出现在 sdist 与 wheel
cd "$REPO"
TAR=$(ls dist/*.tar.gz) WHL=$(ls dist/*.whl)
! tar -tzf "$TAR" | grep -E "governance|arbitrate|\.env$|warehouse/|dashboard/|docs/|benchmark/|tests/" \
  || { echo "sdist 含不应发布的文件"; exit 1; }
! unzip -l "$WHL" | grep -E "governance|arbitrate" || { echo "wheel 含治理模块"; exit 1; }
"$PY" - <<'PYEOF'
import subprocess, sys, tempfile, venv, os, glob
d = tempfile.mkdtemp(); venv.create(d, with_pip=False)
whl = glob.glob("dist/*.whl")[0]
subprocess.run(["uv", "pip", "install", "-q", "--python", os.path.join(d, "bin", "python"), whl], check=True)
r = subprocess.run([os.path.join(d, "bin", "python"), "-c",
    "import metriclens.synth as s; assert s.governance_scan is None; "
    "import metriclens.cli, metriclens.drift, metriclens.render; print('smoke ok')"],
    capture_output=True, text=True)
print(r.stdout.strip() or r.stderr.strip()); sys.exit(r.returncode)
PYEOF
echo "公开发行版就绪: $TAR $WHL"
