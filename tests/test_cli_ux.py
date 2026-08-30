# -*- coding: utf-8 -*-
"""CLI 使用体验:统一异常出口(常见错误一行文案,不裸奔堆栈)、--version、
治理子命令按组件存在性注册、无 catalog 静默降级点名。"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fineprint.cli as cli  # noqa: E402
from fineprint.cli import _err_text, _unknown_sources, main  # noqa: E402
from fineprint.lineage import build_graph  # noqa: E402
from tests.test_generalize import _cat, _node, make_project  # noqa: E402


class TestErrorExit:
    def test_missing_artifacts_is_one_line(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("FINEPRINT_DEBUG", raising=False)
        rc = main(["graph", "--project", str(tmp_path)])
        err = capsys.readouterr().err
        assert rc == 1
        assert "错误" in err and "manifest.json" in err
        assert "Traceback" not in err
        assert "FINEPRINT_DEBUG" in err                    # 提示如何拿到堆栈

    def test_report_without_batch(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("FINEPRINT_DEBUG", raising=False)
        p = make_project(tmp_path, nodes={"model.p.stg": _node("stg", "c/stg.sql")},
                         catalog_nodes={"model.p.stg": _cat("main", "stg", {"a": "INT"})},
                         sqls={"c/stg.sql": "select 1 as a"})
        rc = main(["report", "--project", str(p.project_dir)])
        err = capsys.readouterr().err
        assert rc == 1 and "没有已发布的口径批次" in err and "Traceback" not in err

    def test_debug_env_reraises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FINEPRINT_DEBUG", "1")
        with pytest.raises(FileNotFoundError):
            main(["graph", "--project", str(tmp_path)])

    def test_keyerror_message_unwrapped(self):
        assert _err_text(KeyError("unknown model: x")) == "unknown model: x"
        assert "3" in _err_text(ValueError("bad: 3"))


class TestVersionFlag:
    def test_version_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as e:
            main(["--version"])
        assert e.value.code == 0
        assert "fineprint" in capsys.readouterr().out


class TestGovernRegistration:
    def test_hidden_when_component_absent(self, capsys, monkeypatch):
        real = importlib.util.find_spec
        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda name, *a: None if name == "fineprint.governance" else real(name, *a))
        with pytest.raises(SystemExit) as e:
            main(["--help"])
        assert e.value.code == 0
        assert "govern" not in capsys.readouterr().out      # CLI 不宣传交付里没有的东西
        with pytest.raises(SystemExit) as e:
            main(["govern", "--project", "x"])
        assert e.value.code == 2                            # argparse invalid choice

    def test_present_when_component_available(self, capsys):
        if importlib.util.find_spec("fineprint.governance") is None:
            pytest.skip("发行版无治理组件")           # 摘除树上运行同一测试子集
        with pytest.raises(SystemExit):
            main(["--help"])
        assert "govern" in capsys.readouterr().out


class TestUnknownSourceWarning:
    def _proj(self, tmp_path, declared):
        cols = {c: {"name": c} for c in (["amt"] if declared else [])}
        src = {"source.p.raw.evt": {"name": "evt", "schema": "main",
                                    "database": None, "columns": cols}}
        return make_project(
            tmp_path, nodes={"model.p.stg": _node("stg", "c/stg.sql")},
            catalog_nodes={}, sources=src,
            sqls={"c/stg.sql": "select amt from main.evt"})

    def test_undeclared_source_named(self, tmp_path):
        p = self._proj(tmp_path, declared=False)
        (p.target_dir / "catalog.json").unlink()
        from fineprint.project import DbtProject
        p = DbtProject(p.project_dir)
        rels = _unknown_sources(p, build_graph(p))
        assert rels == [".main.evt"]

    def test_declared_source_quiet(self, tmp_path):
        p = self._proj(tmp_path, declared=True)
        rels = _unknown_sources(p, build_graph(p))
        assert rels == []

    def test_cmd_graph_prints_warning(self, tmp_path, capsys):
        p = self._proj(tmp_path, declared=False)
        (p.target_dir / "catalog.json").unlink()

        class A:
            project = str(p.project_dir)
            target_path = None
            allow_partial = True
        cli.cmd_graph(A())
        err = capsys.readouterr().err
        assert "源表列集未知" in err and "main.evt" in err and "VERIFIED" in err
