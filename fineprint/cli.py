#!/usr/bin/env python3
"""fineprint 命令行入口。

    fineprint init    --project <dbt 项目目录>     生成 fineprint.yml 模板
    fineprint graph   --project DIR                构建字段级血缘图(需先 dbt compile + docs generate)
    fineprint trace   --project DIR model.column   口径树回溯(--full 附出处明细)
    fineprint synth   --project DIR [--only KEY]   双通道口径合成,整批原子发布
    fineprint drift   --project DIR [--strict]     口径快照对比,漂移事件入日志
    fineprint govern  --project DIR                指纹扫描 + B 档 LLM 仲裁 → 治理报告
    fineprint report  --project DIR [-o FILE]      口径卡导出为自包含 HTML
"""
import argparse
import importlib.util
import os
import sys
import warnings
from pathlib import Path

from fineprint.i18n import peek_project_lang, t

DOC_EN = """the fineprint command line.

    fineprint init    --project <dbt project dir>  write a fineprint.yml template
    fineprint graph   --project DIR                build the column-level lineage graph (after dbt compile + docs generate)
    fineprint trace   --project DIR model.column   caliber tree for one column (--full adds receipts)
    fineprint synth   --project DIR [--only KEY]   dual-channel caliber synthesis, atomic batch publish
    fineprint drift   --project DIR [--strict]     caliber snapshot diff, drift events logged
    fineprint govern  --project DIR                fingerprint scan + LLM arbitration → governance report
    fineprint report  --project DIR [-o FILE]      export caliber cards as self-contained HTML
"""


def _project(args):
    from fineprint.project import DbtProject
    return DbtProject(args.project, target_dir=getattr(args, "target_path", None))


def _cfg(args):
    from fineprint.config import MLConfig
    return MLConfig.load(Path(args.project))


def _graph(project):
    from fineprint.trace import load_graph
    p = project.graph_path()
    if not p.exists():
        print(t(f"血缘图不存在({p});请先执行 fineprint graph",
                f"lineage graph not found ({p}); run fineprint graph first"), file=sys.stderr)
        sys.exit(1)
    return load_graph(p)


def _exposure_candidates(project) -> str:
    """init 预填:按 dbt exposures 圈出看板出口模型,其数值度量列作为指标候选
    (注释形态,取消注释即用)。exposure 依赖是模型级,最后一步圈列仍须人工确认。"""
    NUM = ("INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL", "NUMBER", "HUGE")

    def cols_of(m):
        for schs in project.schema.values():
            cols = (schs.get(m["schema"]) or {}).get(m["alias"])
            if cols:
                return cols
        return {}

    lines = []
    for e in list(project.exposures.values())[:10]:
        if not e["models"]:
            continue
        head = f"# ── exposure {e['name']}({e.get('type') or '-'})"
        if e.get("url"):
            head += f"  {e['url']}"
        lines.append(head)
        for uid in e["models"]:
            m = project.models[uid]
            meas = [c for c, ty in cols_of(m).items()
                    if any(t in str(ty).upper() for t in NUM)
                    and not c.lower().endswith(("_id", "_key")) and c.lower() != "id"]
            for c in meas[:8]:
                lines += [f"#   - key: {m['name']}_{c}",
                          f"#     title: {m['name']}.{c}",
                          f"#     target: {m['name']}.{c}"]
            if len(meas) > 8:
                lines.append(f"#   …… {m['name']} 另有 {len(meas) - 8} 个数值列")
    if not lines:
        return ""
    return ("\n# 指标候选(自动发现自 dbt exposures 声明的看板出口模型;\n"
            "# 挪进上方 metrics 列表并取消注释即生效)\n" + "\n".join(lines) + "\n")


def cmd_init(args):
    from fineprint.config import example_yml
    f = Path(args.project) / "fineprint.yml"
    if f.exists() and not args.force:
        print(t(f"{f} 已存在(--force 覆盖)", f"{f} already exists (--force to overwrite)"))
        return
    text, tip = example_yml(), ""
    try:
        block = _exposure_candidates(_project(args))
        if block:
            text += block
            tip = t(";已按 dbt exposures 预填指标候选(文件尾部注释)",
                    "; metric candidates pre-filled from dbt exposures (commented, end of file)")
    except Exception:
        pass          # artifacts 尚未编译时 init 仍可用,只出模板
    f.write_text(text)
    print(t(f"已生成 {f}\n下一步:填入 metrics(model.column){tip},"
            f"设置 LLM 环境变量(见 README),然后 fineprint graph",
            f"wrote {f}\nnext: fill in metrics (model.column){tip}, "
            f"set the LLM env vars (see README), then run fineprint graph"))


def _unknown_sources(project, graph) -> list:
    """被血缘引用、但列集完全未知的源表(catalog 未覆盖且 sources yml 未声明列)。
    这些表前血缘截止,列归属只能靠拓扑推断,口径卡难以达到 VERIFIED——
    静默降级须点名,不能让用户从卡片置信度倒推原因。"""
    used = set()
    for m in graph["models"].values():
        used.update(m["row_set_tables"])
        for c in m["columns"].values():
            used.update(u["table"] for u in c["upstreams"])
    srcs = graph["relations"]["sources"]
    out = []
    for rel in sorted(used):
        parts = rel.split(".")
        if rel not in srcs or len(parts) != 3:
            continue
        db, sch, tbl = parts
        if not ((project.schema.get(db) or {}).get(sch) or {}).get(tbl):
            out.append(rel)
    return out


def cmd_graph(args):
    from fineprint.lineage import build_graph, save_graph
    project = _project(args)
    if project.catalog_missing:
        print(t("⚠ 未找到 catalog.json,进入无 catalog 模式:列 schema 由 yml 声明 + "
                "编译 SQL 拓扑推断补全;能执行 dbt docs generate 时仍建议补上(实测列集更强)",
                "⚠ catalog.json not found — entering no-catalog mode: column schema comes from "
                "yml declarations + topological inference over compiled SQL; when you can run "
                "dbt docs generate, do — measured column sets are stronger"),
              file=sys.stderr)
    graph = build_graph(project)
    unknown = _unknown_sources(project, graph)
    if unknown:
        sep = t("、", ", ")
        shown = sep.join(unknown[:6]) + (t(f" ……共 {len(unknown)} 个",
                                           f" … {len(unknown)} total") if len(unknown) > 6 else "")
        print(t(f"⚠ {len(unknown)} 个被引用的源表列集未知(catalog 未覆盖且 sources yml 未声明列):\n"
                f"    {shown}\n"
                f"  血缘在这些表前截止,列归属仅靠拓扑推断,相关口径卡难以达到 VERIFIED;\n"
                f"  修复:执行 dbt docs generate 生成 catalog,或在 sources yml 为上述表声明 columns",
                f"⚠ {len(unknown)} referenced source table(s) have unknown column sets "
                f"(not in catalog, no columns declared in sources yml):\n"
                f"    {shown}\n"
                f"  lineage stops at these tables and column attribution relies on topology alone, "
                f"so affected cards are unlikely to reach VERIFIED;\n"
                f"  fix: run dbt docs generate, or declare columns for these tables in sources yml"),
              file=sys.stderr)
    ncols = sum(len(m["columns"]) for m in graph["models"].values())
    nconds = sum(len(m["conditions"]) for m in graph["models"].values())
    nsem = sum(len(m["semantics"]) for m in graph["models"].values())
    errs = [(n, c) for n, m in graph["models"].items() for c, d in m["columns"].items() if d.get("error")]
    errs += [(n, f"<model: {m['error'][:60]}>") for n, m in graph["models"].items() if m.get("error")]
    if errs and not args.allow_partial:
        # 校验不过不落盘:失败运行不得覆盖上一次可用的图(trace/synth 仍读旧图)
        print(f"column lineage errors ({len(errs)}):", errs[:8], file=sys.stderr)
        print(t("图未写出,旧图保持不变;确认可接受后用 --allow-partial 强制写出",
                "graph not written, the previous graph is kept; rerun with --allow-partial "
                "once you have confirmed the errors are acceptable"), file=sys.stderr)
        sys.exit(1)
    save_graph(project, graph)
    print(f"graph: {len(graph['models'])} models, {ncols} columns, {nconds} conditions, "
          f"{nsem} semantic points → {project.graph_path()}  (dialect={graph['meta']['dialect']})")
    if errs:
        print(f"column lineage errors ({len(errs)}, --allow-partial):", errs[:8], file=sys.stderr)


def cmd_trace(args):
    from fineprint.trace import render, resolve_model, trace
    project = _project(args)
    graph = _graph(project)
    model, col = args.target.rsplit(".", 1)
    t = trace(graph, model, col)
    tree_txt = None
    try:                                  # 口径树是展示增强:失败静默回退平铺视图
        from fineprint.tree import caliber_tree, render_tree
        tr = caliber_tree(project, graph, resolve_model(graph, model), col, t)
        if tr:
            tree_txt = render_tree(tr)
    except Exception:
        tree_txt = None
    print(render(t, tree=tree_txt, full=args.full))


def cmd_synth(args):
    from fineprint.llm import load_dotenv
    from fineprint.synth import Progress, run_all
    project = _project(args)
    load_dotenv(project.project_dir)
    cfg = _cfg(args)
    prog = Progress(mode="json" if args.json else "human", verbose=args.verbose)
    sys.exit(run_all(project, cfg, _graph(project), only=args.only, progress=prog))


def cmd_drift(args):
    from fineprint.drift import print_events, run_check
    project = _project(args)
    cfg = _cfg(args)
    events = run_check(project, cfg, _graph(project), save=not args.dry_run,
                       block_high=args.strict)
    print_events(events)
    if args.strict and any(e["severity"] == "high" for e in events):
        sys.exit(1)


def cmd_govern(args):
    try:
        from fineprint.arbitrate import build_report, print_report
    except ImportError:
        print(t("此发行版未包含治理组件(重复建设扫描与仲裁);其余命令不受影响。",
                "this distribution does not include the governance component "
                "(duplicate-build scan & arbitration); all other commands are unaffected."),
              file=sys.stderr)
        return 3
    from fineprint.llm import load_dotenv
    project = _project(args)
    load_dotenv(project.project_dir)
    cfg = _cfg(args)
    print_report(build_report(project, cfg, _graph(project)))


def cmd_report(args):
    from fineprint.report import export_html
    project = _project(args)
    out = Path(args.output) if args.output else project.workspace / "caliber_report.html"
    n = export_html(project, out)
    print(t(f"报告已导出: {out}({n} 张口径卡)",
            f"report exported: {out} ({n} caliber cards)"))


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("fineprint")
    except Exception:
        return "0+unknown"


def _err_text(e: BaseException) -> str:
    # KeyError 的 str() 会给消息包上引号,取原始 args 还原文案
    if isinstance(e, KeyError) and len(e.args) == 1 and isinstance(e.args[0], str):
        return e.args[0]
    return str(e)


def _bootstrap_lang(argv):
    """建 parser 之前解析语言:--help 也要说对语言。--project 从 argv 裸扫
    (此时还没有 argparse),窥探该项目 fineprint.yml 的 language 键;
    env FINEPRINT_LANG 始终最高优先(i18n 层保证)。"""
    proj = "."
    for i, a in enumerate(argv):
        if a == "--project" and i + 1 < len(argv):
            proj = argv[i + 1]
        elif a.startswith("--project="):
            proj = a.split("=", 1)[1]
    peek_project_lang(proj)


def main(argv=None):
    # urllib3 在 LibreSSL 环境(macOS 系统 Python)每次 import 都告警一次,
    # 与用户操作无关,按消息精确静默(不整类屏蔽,其余 urllib3 告警照常)
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
    _bootstrap_lang(argv if argv is not None else sys.argv[1:])
    has_govern = importlib.util.find_spec("fineprint.governance") is not None
    doc = t(__doc__, DOC_EN)
    if not has_govern:
        doc = "\n".join(line for line in doc.splitlines() if "govern" not in line)
    ap = argparse.ArgumentParser(prog="fineprint", description=doc,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--project", default=".",
                       help=t("dbt 项目根目录(默认当前目录)",
                              "dbt project root (default: current directory)"))
        p.add_argument("--target-path", dest="target_path",
                       help=t("dbt target 目录(默认: DBT_TARGET_PATH → dbt_project.yml 的 target-path → target)",
                              "dbt target dir (default: DBT_TARGET_PATH → target-path in dbt_project.yml → target)"))
        return p

    p = common(sub.add_parser("init", help=t("生成 fineprint.yml 模板",
                                             "write a fineprint.yml template")))
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    p = common(sub.add_parser("graph", help=t("构建字段级血缘图",
                                              "build the column-level lineage graph")))
    p.add_argument("--allow-partial", action="store_true",
                   help=t("存在解析失败的列时仍以 0 退出",
                          "exit 0 even when some columns failed to parse"))
    p.set_defaults(fn=cmd_graph)
    p = common(sub.add_parser("trace", help=t("口径树回溯(--full 附出处明细)",
                                              "caliber tree for one column (--full adds receipts)")))
    p.add_argument("target", help="model.column")
    p.add_argument("--full", action="store_true",
                   help=t("在口径树下附完整出处明细(表达式链 E / 源字段 S / 逐条过滤条件 F)",
                          "append full receipts under the tree (expression chain E / sources S / filters F)"))
    p.set_defaults(fn=cmd_trace)
    p = common(sub.add_parser("synth", help=t("双通道口径合成(LLM)",
                                              "dual-channel caliber synthesis (LLM)")))
    p.add_argument("--only", help=t("只重跑一个指标 key(从 active 批次补齐其余)",
                                    "re-run a single metric key (backfill the rest from the active batch)"))
    p.add_argument("-v", "--verbose", action="store_true",
                   help=t("阶段进度附更多细节(逐跳模型清单等)",
                          "more per-stage detail (per-hop model lists, …)"))
    p.add_argument("--json", action="store_true",
                   help=t("stdout 逐行输出 JSON 进度事件(供 CI/脚本消费)",
                          "emit JSON progress events line-by-line on stdout (for CI/scripts)"))
    p.set_defaults(fn=cmd_synth)
    p = common(sub.add_parser("drift", help=t("口径漂移检测", "caliber drift check")))
    p.add_argument("--strict", action="store_true",
                   help=t("high 级漂移非零退出", "exit non-zero on high-severity drift"))
    p.add_argument("--dry-run", action="store_true",
                   help=t("只对比不落盘", "compare only, write nothing"))
    p.set_defaults(fn=cmd_drift)
    if has_govern:   # 治理组件未随发行版打包时不注册子命令:CLI 不宣传交付里没有的东西
        common(sub.add_parser("govern",
                              help=t("指纹扫描 + LLM 仲裁 → 治理报告",
                                     "fingerprint scan + LLM arbitration → governance report"))
               ).set_defaults(fn=cmd_govern)
    p = common(sub.add_parser("report", help=t("口径卡导出 HTML",
                                               "export caliber cards as HTML")))
    p.add_argument("-o", "--output",
                   help=t("输出文件(默认 .fineprint/caliber_report.html)",
                          "output file (default .fineprint/caliber_report.html)"))
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print(t("\n已中断", "\ninterrupted"), file=sys.stderr)
        return 130
    except Exception as e:
        # 常见使用错误(缺 artifacts/配置、目标写错、批次未发布……)的异常文案
        # 本身已可行动,统一在此收口成一行提示;真正的缺陷才需要堆栈,
        # FINEPRINT_DEBUG=1 原样抛出
        if os.environ.get("FINEPRINT_DEBUG"):
            raise
        expected = isinstance(e, (FileNotFoundError, ValueError, KeyError, RuntimeError))
        head = (t("错误", "error") if expected
                else t(f"内部错误({type(e).__name__})", f"internal error ({type(e).__name__})"))
        print(f"{head}: {_err_text(e)}", file=sys.stderr)
        print(t("(FINEPRINT_DEBUG=1 可查看完整堆栈)",
                "(set FINEPRINT_DEBUG=1 for the full traceback)"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
