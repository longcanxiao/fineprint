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


class TestFirstRunFriction:
    """首轮陌生用户试用反馈(2026-08-31):init 提示顺序、模板治理段、
    synth 预检、公开 API 语言继承、旧批次报告标记。
    夹具全合成——不依赖 examples/quickstart/.fineprint(未跟踪,CI 没有)。"""

    def test_init_hint_orders_llm_after_graph(self, tmp_path, capsys):
        main(["init", "--project", str(tmp_path)])
        out = capsys.readouterr().out
        assert "零 LLM" in out
        # 建图在前,凭据在后:graph/trace 不需要 LLM,提示不能倒装成先配凭据
        assert out.index("fineprint graph") < out.index("LLM 环境变量")
        assert "synth" in out

    def test_example_yml_governance_follows_component(self, monkeypatch):
        from fineprint.config import example_yml
        has_gov = importlib.util.find_spec("fineprint.governance") is not None
        assert ("governance:" in example_yml()) == has_gov
        real = importlib.util.find_spec
        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda name, *a: None if name == "fineprint.governance" else real(name, *a))
        assert "governance:" not in example_yml()   # 发行版模板不宣传交付里没有的配置

    def test_synth_preflight_fails_before_batch(self, tmp_path, capsys, monkeypatch):
        from fineprint import llm
        for k in ("FINEPRINT_LLM_API_KEY", "OPENAI_API_KEY", "FINEPRINT_LLM_MODEL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv("FINEPRINT_DEBUG", raising=False)
        p = make_project(tmp_path, nodes={"model.p.stg": _node("stg", "c/stg.sql")},
                         catalog_nodes={"model.p.stg": _cat("main", "stg", {"a": "INT"})},
                         sqls={"c/stg.sql": "select 1 as a"})
        llm.settings.cache_clear()
        try:
            rc = main(["synth", "--project", str(p.project_dir)])
        finally:
            llm.settings.cache_clear()
        err = capsys.readouterr().err
        assert rc == 1
        assert "FINEPRINT_LLM_API_KEY" in err and "FINEPRINT_LLM_MODEL" in err  # 缺失项一次列全
        assert "Traceback" not in err
        assert "▶" not in err                                       # 没打开工横幅
        assert not (p.project_dir / ".fineprint" / "store").exists()  # 预检先于建批次

    def test_api_inherits_project_language(self, tmp_path, monkeypatch):
        import fineprint
        from fineprint import i18n
        monkeypatch.delenv("FINEPRINT_LANG", raising=False)
        for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(i18n, "_LANG", None)
        p = make_project(tmp_path, nodes={"model.p.stg": _node("stg", "c/stg.sql")},
                         catalog_nodes={"model.p.stg": _cat("main", "stg", {"a": "INT"})},
                         sqls={"c/stg.sql": "select 1 as a"})
        (p.project_dir / "fineprint.yml").write_text("language: zh\nmetrics: []\n",
                                                     encoding="utf-8")
        fineprint.build_graph(str(p.project_dir))
        assert i18n.lang() == "zh"           # 语言随项目配置,与 CLI 同一来源
        r = fineprint.trace(str(p.project_dir), "stg.a")
        assert i18n.lang() == "zh" and str(r)  # Notebook 输出跟着项目语言走

    def test_api_language_en_project(self, tmp_path, monkeypatch):
        import fineprint
        from fineprint import i18n
        monkeypatch.delenv("FINEPRINT_LANG", raising=False)
        for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(i18n, "_LANG", None)
        (tmp_path / "fineprint.yml").write_text("language: en\nmetrics: []\n",
                                                encoding="utf-8")
        with pytest.raises(FileNotFoundError) as e:
            fineprint.cards(str(tmp_path))
        assert "no published definition batch" in str(e.value)

    def test_report_marks_legacy_batch(self, tmp_path):
        import json
        from fineprint.project import DbtProject
        from fineprint.report import _stale_banner, export_html
        from fineprint.store import CARD_SCHEMA_VERSION, CaliberStore
        from tests.test_report import _card
        assert _stale_banner({"schema_version": CARD_SCHEMA_VERSION}) == ""
        p = make_project(tmp_path, nodes={"model.p.stg": _node("stg", "c/stg.sql")},
                         catalog_nodes={"model.p.stg": _cat("main", "stg", {"a": "INT"})},
                         sqls={"c/stg.sql": "select 1 as a"})
        store = CaliberStore(p.project_dir / ".fineprint" / "store")
        d = store.run_dir("legacy1")
        (d / "gmv.json").write_text(json.dumps(_card(), ensure_ascii=False),
                                    encoding="utf-8")
        # 0.9 前的批次索引没有 schema_version —— 兼容展示但页头必须显眼标记
        (d / "index.json").write_text(json.dumps({"run_id": "legacy1", "at": "t",
                                                  "cards": ["gmv"]}), encoding="utf-8")
        store.activate("legacy1")
        out = tmp_path / "r.html"
        export_html(DbtProject(p.project_dir), out)
        h = out.read_text(encoding="utf-8")
        assert 'class="stale"' in h and "旧版批次" in h

    def test_shipped_quickstart_batch_is_current(self):
        # 内置批次必须现世代——否则新用户开箱第一眼就是"旧版批次"横幅
        from fineprint.store import CARD_SCHEMA_VERSION, CaliberStore
        store = CaliberStore(ROOT / "examples/quickstart/.fineprint/store")
        if store.active_dir() is None:
            pytest.skip("quickstart 内置批次不在(sdist 场景)")
        assert (store.index() or {}).get("schema_version") == CARD_SCHEMA_VERSION

    def test_init_warns_outside_dbt_project(self, tmp_path, capsys):
        main(["init", "--project", str(tmp_path)])
        err = capsys.readouterr().err
        assert "dbt_project.yml" in err          # 空目录不再静默成功:点名这不是 dbt 工程根
        assert (tmp_path / "fineprint.yml").exists()   # 但模板照常生成

    def test_drift_first_run_no_contradiction(self, tmp_path, capsys):
        p = make_project(tmp_path, nodes={"model.p.stg": _node("stg", "c/stg.sql")},
                         catalog_nodes={"model.p.stg": _cat("main", "stg", {"a": "INT"})},
                         sqls={"c/stg.sql": "select 1 as a"})
        (p.project_dir / "fineprint.yml").write_text(
            "language: zh\nmetrics:\n  - key: a\n    title: A\n    target: stg.a\n",
            encoding="utf-8")
        main(["graph", "--project", str(p.project_dir)])
        main(["drift", "--project", str(p.project_dir)])
        out = capsys.readouterr().out
        assert "基线" in out
        # 首跑=建基线,没有"对比"这回事,不得再并列一句"无变化"
        assert "无变化" not in out and "no changes" not in out

    def test_init_demo_full_loop(self, tmp_path, capsys):
        main(["init", "--demo", "--project", str(tmp_path)])
        d = tmp_path / "fineprint-quickstart"
        assert (d / "target" / "manifest.json").exists()
        assert (d / ".fineprint" / "store" / "active_run").exists()  # 内置批次随包走
        main(["graph", "--project", str(d)])
        assert (d / ".fineprint" / "graph.json").exists()
        rc = main(["init", "--demo", "--project", str(tmp_path)])    # 已存在:一行报错
        err = capsys.readouterr().err
        assert rc == 1 and "fineprint-quickstart" in err and "Traceback" not in err

    def test_init_demo_speaks_english_on_zh_locale(self, tmp_path, capsys, monkeypatch):
        # demo 外壳语言=demo 内容语言(en):zh locale 不该让 init --demo 出中文,
        # 否则 cd 进去下一条命令就切英文,三十秒内语言精分。显式 FINEPRINT_LANG 仍最高。
        from fineprint import i18n
        monkeypatch.delenv("FINEPRINT_LANG", raising=False)
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        monkeypatch.setattr(i18n, "_LANG", None)
        main(["init", "--demo", "--project", str(tmp_path)])
        out = capsys.readouterr().out
        assert "demo project written to" in out and "示例工程" not in out

        monkeypatch.setenv("FINEPRINT_LANG", "zh")
        monkeypatch.setattr(i18n, "_LANG", None)
        rc = main(["init", "--demo", "--project", str(tmp_path)])   # 已存在:报错也该按显式语言
        err = capsys.readouterr().err
        assert rc == 1 and "已存在" in err

    def test_demo_matches_quickstart_tracked_files(self):
        # _demo=examples/quickstart 发行拷贝,必须与 git 跟踪集逐字节一致(scripts/sync_demo.py)
        import subprocess
        try:
            out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "examples/quickstart"],
                                 capture_output=True, text=True, check=True).stdout
        except Exception:
            pytest.skip("无 git(非仓库环境)")
        tracked = {ln.split("examples/quickstart/", 1)[1]
                   for ln in out.splitlines() if ln.strip()}
        if not tracked:
            pytest.skip("git 未跟踪 quickstart(非仓库环境)")
        demo = ROOT / "fineprint" / "_demo"
        demo_files = {p.relative_to(demo).as_posix() for p in demo.rglob("*") if p.is_file()}
        assert demo_files == tracked
        for rel in sorted(tracked):
            assert (demo / rel).read_bytes() == \
                (ROOT / "examples" / "quickstart" / rel).read_bytes(), rel


class TestColumns:
    """columns=候选发现命令:零 LLM,读图列 model.column;新用户从 init 到 trace 的断链补丁。"""

    def _proj(self, tmp_path):
        return make_project(
            tmp_path,
            nodes={"model.p.stg_pay": _node("stg_pay", "c/s.sql"),
                   "model.p.dm_rate": _node("dm_rate", "c/d.sql")},
            catalog_nodes={"model.p.stg_pay": _cat("main", "stg_pay",
                                                   {"amt": "DOUBLE", "user_id": "INTEGER",
                                                    "status": "VARCHAR"}),
                           "model.p.dm_rate": _cat("main", "dm_rate",
                                                   {"rate": "DOUBLE", "d": "DATE"})},
            sqls={"c/s.sql": "select 1.0 as amt, 7 as user_id, 'x' as status",
                  "c/d.sql": "select 0.5 as rate, current_date as d"})

    def test_overview_then_keyword(self, tmp_path, capsys):
        p = self._proj(tmp_path)
        main(["graph", "--project", str(p.project_dir)])
        out = capsys.readouterr().out
        assert "fineprint columns" in out                 # graph 尾巴指路
        main(["columns", "--project", str(p.project_dir)])
        out = capsys.readouterr().out
        assert "stg_pay" in out and "dm_rate" in out and "3 " in out   # 概览含列数
        main(["columns", "rate", "--project", str(p.project_dir)])
        out = capsys.readouterr().out
        assert "rate" in out and "数值" in out            # 关键词展开+数值标
        assert "stg_pay" not in out                       # 不匹配的模型不出现
        assert "fineprint trace dm_rate.rate" in out      # 用法行给出可直接复制的目标

    def test_model_flag_and_id_exclusion(self, tmp_path, capsys):
        p = self._proj(tmp_path)
        main(["graph", "--project", str(p.project_dir)])
        capsys.readouterr()
        main(["columns", "--model", "stg_pay", "--project", str(p.project_dir)])
        out = capsys.readouterr().out
        amt_line = next(ln for ln in out.splitlines() if " amt" in ln)
        id_line = next(ln for ln in out.splitlines() if "user_id" in ln)
        assert "数值" in amt_line
        assert "数值" not in id_line                      # ID 列不标指标候选

    def test_without_graph_points_at_graph(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("FINEPRINT_DEBUG", raising=False)
        p = self._proj(tmp_path)
        rc = main(["columns", "--project", str(p.project_dir)])
        err = capsys.readouterr().err
        assert rc == 1 and "fineprint graph" in err and "Traceback" not in err
