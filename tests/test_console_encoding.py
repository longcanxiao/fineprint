# -*- coding: utf-8 -*-
"""窄编码控制台(Windows cp1252/GBK)下 CLI 不得因树形字符/中文崩溃:
输出策略 = errors="replace"(字符降级成 ?),退出码与流程不变。"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(args, lang, tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "cp1252",  # 模拟窄编码控制台
           "FINEPRINT_LANG": lang, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "fineprint.cli", *args],
                          capture_output=True, text=True, errors="replace",
                          cwd=tmp_path, env=env)


class TestNarrowConsole:
    def test_chinese_error_degrades_not_crashes(self, tmp_path):
        # 无 manifest → 中文错误文案;cp1252 编不出中文,须降级输出而非 UnicodeEncodeError
        r = _run(["graph", "--project", "."], "zh", tmp_path)
        assert r.returncode == 1
        assert "UnicodeEncodeError" not in r.stderr
        assert "?" in r.stderr                 # 中文被 replace 成 ?,信息通道仍在

    def test_english_error_readable(self, tmp_path):
        r = _run(["graph", "--project", "."], "en", tmp_path)
        assert r.returncode == 1
        assert "UnicodeEncodeError" not in r.stderr
        assert "manifest.json" in r.stderr     # 英文文案在窄编码下完整可读
