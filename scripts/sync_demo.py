#!/usr/bin/env python3
"""examples/quickstart(体验工程唯一事实源)→ fineprint/_demo(随包发行的拷贝)。

`fineprint init --demo` 从包内 _demo 落一份可跑的示例工程——纯 pip 用户
不必检出仓库就能见到效果。同步集合 = examples/quickstart 的 git 跟踪文件
(与 GitHub 用户所见严格一致;.env/缓存/报告/duckdb/日志等本地态天然不入)。

改动示例后运行本脚本再提交;tests/test_cli_ux.py 的同步守卫强制两树一致。
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "examples" / "quickstart"
DST = ROOT / "fineprint" / "_demo"


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "examples/quickstart"],
                         capture_output=True, text=True, check=True).stdout
    return [line.split("examples/quickstart/", 1)[1]
            for line in out.splitlines() if line.strip()]


def sync() -> int:
    files = tracked_files()
    if not files:
        sys.exit("git ls-files 返回空:请在仓库内运行")
    if DST.exists():
        shutil.rmtree(DST)
    for rel in files:
        dst = DST / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC / rel, dst)
    return len(files)


if __name__ == "__main__":
    print(f"synced {sync()} files → {DST}")
