# -*- coding: utf-8 -*-
"""i18n 解析次序与 synth 进度上报(双语/JSON 事件/重试钩子)。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fineprint.i18n as i18n  # noqa: E402
from fineprint import llm  # noqa: E402
from fineprint.cli import main  # noqa: E402
from fineprint.synth import STAGES, Progress  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_lang(monkeypatch):
    monkeypatch.setattr(i18n, "_LANG", None)
    yield
    i18n._LANG = None


class TestLangResolution:
    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("FINEPRINT_LANG", "en")
        i18n.set_lang("zh")
        assert i18n.lang() == "en" and i18n.t("中", "e") == "e"

    def test_config_beats_locale(self, monkeypatch):
        monkeypatch.delenv("FINEPRINT_LANG", raising=False)
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        i18n.set_lang("en")
        assert i18n.lang() == "en"

    def test_locale_then_english_default(self, monkeypatch):
        monkeypatch.delenv("FINEPRINT_LANG", raising=False)
        for k in ("LC_ALL", "LC_MESSAGES"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        assert i18n.lang() == "zh"
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        assert i18n.lang() == "en"          # 无中文信号默认英文:首批用户国际化
        i18n.set_lang("fr")                  # 非法值忽略
        assert i18n.lang() == "en"

    def test_peek_project_yml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FINEPRINT_LANG", raising=False)
        (tmp_path / "fineprint.yml").write_text("language: en\nmetrics: []\n")
        i18n.peek_project_lang(tmp_path)
        assert i18n.lang() == "en"
        i18n.peek_project_lang(tmp_path / "nope")   # 不存在:安静跳过
        assert i18n.lang() == "en"


class TestProgress:
    def test_json_mode_events(self, capsys):
        p = Progress(mode="json", verbose=False)
        p.emit("stage", key="gmv", stage="merge")
        p.emit("stage", key="gmv", stage="extract", verbose_only=True, detail="x")
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1                    # verbose_only 事件默认吞掉
        ev = json.loads(out[0])
        assert ev["event"] == "stage" and ev["key"] == "gmv" and "elapsed_s" in ev

    def test_human_mode_bilingual(self, capsys, monkeypatch):
        p = Progress()
        p.emit("stage", key="k", stage="business")
        monkeypatch.setenv("FINEPRINT_LANG", "en")
        p.emit("stage", key="k", stage="business")
        err = capsys.readouterr().err
        assert "业务口径生成" in err and "generating business definition" in err

    def test_retry_hook_routes_into_progress(self, capsys):
        p = Progress(mode="json")
        old = llm.on_retry
        try:
            p.install_retry_hook()
            llm.on_retry({"attempt": 1, "max": 8, "model": "m", "error": "HTTP 429", "wait": 2.0})
        finally:
            llm.on_retry = old
        ev = json.loads(capsys.readouterr().out)
        assert ev["event"] == "retry" and ev["attempt"] == 1

    def test_stage_catalog_complete(self):
        assert set(STAGES) == {"trace", "extract", "merge", "business", "validate"}


class TestEnglishCli:
    def test_error_exit_english(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("FINEPRINT_LANG", "en")
        monkeypatch.delenv("FINEPRINT_DEBUG", raising=False)
        rc = main(["graph", "--project", str(tmp_path)])
        err = capsys.readouterr().err
        assert rc == 1 and "error:" in err and "FINEPRINT_DEBUG" in err
        assert "错误" not in err

    def test_help_english(self, capsys, monkeypatch):
        monkeypatch.setenv("FINEPRINT_LANG", "en")
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        # 0.9.7 起英文面弃用 caliber(中英假朋友):en 帮助既要是英文,也不得再出现该词
        assert "definition" in out and "口径" not in out and "caliber" not in out

    def test_help_follows_project_yml(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("FINEPRINT_LANG", raising=False)
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        (tmp_path / "fineprint.yml").write_text("language: zh\n")
        with pytest.raises(SystemExit):
            main(["synth", "--project", str(tmp_path), "--help"])
        assert "项目根目录" in capsys.readouterr().out   # --help 也吃项目配置的语言
