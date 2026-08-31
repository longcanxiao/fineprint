# -*- coding: utf-8 -*-
"""0.8.4 品牌统一的升级体验:版本号单一事实源、旧名残留检测与指路提示。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fineprint  # noqa: E402
from fineprint import llm  # noqa: E402
from fineprint.cli import _graph, main  # noqa: E402
from fineprint.config import MLConfig  # noqa: E402


class TestVersionSingleSource:
    def test_dunder_version_matches_metadata(self):
        from importlib.metadata import version
        assert fineprint.__version__ == version("fineprint")   # 不再是硬编码副本

    def test_cli_version_same_source(self, capsys):
        with pytest.raises(SystemExit):
            main(["--version"])
        assert fineprint.__version__ in capsys.readouterr().out


class TestLegacyNameHints:
    def test_config_hint_when_metriclens_yml_present(self, tmp_path):
        (tmp_path / "metriclens.yml").write_text("language: zh\n")
        with pytest.raises(FileNotFoundError) as e:
            MLConfig.load(tmp_path)
        msg = str(e.value)
        assert "metriclens.yml" in msg and "mv metriclens.yml fineprint.yml" in msg

    def test_config_no_hint_without_legacy_file(self, tmp_path):
        with pytest.raises(FileNotFoundError) as e:
            MLConfig.load(tmp_path)
        assert "metriclens" not in str(e.value)

    def test_graph_hint_when_legacy_workspace_present(self, tmp_path, capsys):
        (tmp_path / ".metriclens").mkdir()
        (tmp_path / ".metriclens" / "graph.json").write_text("{}")

        class P:
            project_dir = tmp_path

            def graph_path(self):
                return tmp_path / ".fineprint" / "graph.json"

        with pytest.raises(FileNotFoundError) as ei:   # 统一异常出口负责变成人话
            _graph(P())
        msg = str(ei.value)
        assert "mv .metriclens .fineprint" in msg and "漂移历史" in msg

    def test_dotenv_warns_on_legacy_keys(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("FINEPRINT_LLM_TEST_TOKEN_X", raising=False)
        (tmp_path / ".env").write_text(
            "METRICLENS_LLM_API_KEY=old\nMETRICLENS_LLM_MODEL=m\n"
            "FINEPRINT_LLM_TEST_TOKEN_X=new\n")
        llm.load_dotenv(tmp_path)
        err = capsys.readouterr().err
        assert "METRICLENS_" in err and "FINEPRINT_" in err       # 点名旧键并指路
        assert __import__("os").environ.get("FINEPRINT_LLM_TEST_TOKEN_X") == "new"
        monkeypatch.delenv("FINEPRINT_LLM_TEST_TOKEN_X", raising=False)

    def test_settings_error_mentions_legacy_env(self, monkeypatch):
        for k in ("FINEPRINT_LLM_API_KEY", "OPENAI_API_KEY", "FINEPRINT_LLM_MODEL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("METRICLENS_LLM_API_KEY", "old")
        llm.settings.cache_clear()
        try:
            with pytest.raises(KeyError) as e:
                llm.settings()
            assert "METRICLENS_LLM_*" in str(e.value)
        finally:
            llm.settings.cache_clear()


class TestSmallFixes:
    def test_init_force_has_help(self, capsys):
        with pytest.raises(SystemExit):
            main(["init", "--help"])
        assert "覆盖" in capsys.readouterr().out
