#!/usr/bin/env python3
"""metriclens 命令行入口。

    metriclens init    --project <dbt 项目目录>     生成 metriclens.yml 模板
    metriclens graph   --project DIR                构建字段级血缘图(需先 dbt compile + docs generate)
    metriclens trace   --project DIR model.column   S/F/E 三元组回溯
    metriclens synth   --project DIR [--only KEY]   双通道口径合成,整批原子发布
    metriclens drift   --project DIR [--strict]     口径快照对比,漂移事件入日志
    metriclens govern  --project DIR                指纹扫描 + B 档 LLM 仲裁 → 治理报告
    metriclens report  --project DIR [-o FILE]      口径卡导出为自包含 HTML
"""
import argparse
import sys
from pathlib import Path


def _project(args):
    from metriclens.project import DbtProject
    return DbtProject(args.project, target_dir=getattr(args, "target_path", None))


def _cfg(args):
    from metriclens.config import MLConfig
    return MLConfig.load(Path(args.project))


def _graph(project):
    from metriclens.trace import load_graph
    p = project.graph_path()
    if not p.exists():
        print(f"血缘图不存在({p});请先执行 metriclens graph", file=sys.stderr)
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
    from metriclens.config import EXAMPLE
    f = Path(args.project) / "metriclens.yml"
    if f.exists() and not args.force:
        print(f"{f} 已存在(--force 覆盖)")
        return
    text, tip = EXAMPLE, ""
    try:
        block = _exposure_candidates(_project(args))
        if block:
            text += block
            tip = ";已按 dbt exposures 预填指标候选(文件尾部注释)"
    except Exception:
        pass          # artifacts 尚未编译时 init 仍可用,只出模板
    f.write_text(text)
    print(f"已生成 {f}\n下一步:填入 metrics(model.column){tip},"
          f"设置 LLM 环境变量(见 README),然后 metriclens graph")


def cmd_graph(args):
    from metriclens.lineage import build_graph, save_graph
    project = _project(args)
    if project.catalog_missing:
        print("⚠ 未找到 catalog.json,进入无 catalog 模式:列 schema 由 yml 声明 + "
              "编译 SQL 拓扑推断补全;能执行 dbt docs generate 时仍建议补上(实测列集更强)",
              file=sys.stderr)
    graph = build_graph(project)
    ncols = sum(len(m["columns"]) for m in graph["models"].values())
    nconds = sum(len(m["conditions"]) for m in graph["models"].values())
    nsem = sum(len(m["semantics"]) for m in graph["models"].values())
    errs = [(n, c) for n, m in graph["models"].items() for c, d in m["columns"].items() if d.get("error")]
    errs += [(n, f"<model: {m['error'][:60]}>") for n, m in graph["models"].items() if m.get("error")]
    if errs and not args.allow_partial:
        # 校验不过不落盘:失败运行不得覆盖上一次可用的图(trace/synth 仍读旧图)
        print(f"column lineage errors ({len(errs)}):", errs[:8], file=sys.stderr)
        print("图未写出,旧图保持不变;确认可接受后用 --allow-partial 强制写出", file=sys.stderr)
        sys.exit(1)
    save_graph(project, graph)
    print(f"graph: {len(graph['models'])} models, {ncols} columns, {nconds} conditions, "
          f"{nsem} semantic points → {project.graph_path()}  (dialect={graph['meta']['dialect']})")
    if errs:
        print(f"column lineage errors ({len(errs)}, --allow-partial):", errs[:8], file=sys.stderr)


def cmd_trace(args):
    from metriclens.trace import render, trace
    project = _project(args)
    graph = _graph(project)
    model, col = args.target.rsplit(".", 1)
    print(render(trace(graph, model, col)))


def cmd_synth(args):
    from metriclens.llm import load_dotenv
    from metriclens.synth import run_all
    project = _project(args)
    load_dotenv(project.project_dir)
    cfg = _cfg(args)
    sys.exit(run_all(project, cfg, _graph(project), only=args.only))


def cmd_drift(args):
    from metriclens.drift import print_events, run_check
    project = _project(args)
    cfg = _cfg(args)
    events = run_check(project, cfg, _graph(project), save=not args.dry_run,
                       block_high=args.strict)
    print_events(events)
    if args.strict and any(e["severity"] == "high" for e in events):
        sys.exit(1)


def cmd_govern(args):
    from metriclens.arbitrate import build_report, print_report
    from metriclens.llm import load_dotenv
    project = _project(args)
    load_dotenv(project.project_dir)
    cfg = _cfg(args)
    print_report(build_report(project, cfg, _graph(project)))


def cmd_report(args):
    from metriclens.report import export_html
    project = _project(args)
    out = Path(args.output) if args.output else project.workspace / "caliber_report.html"
    n = export_html(project, out)
    print(f"报告已导出: {out}({n} 张口径卡)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="metriclens", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--project", default=".", help="dbt 项目根目录(默认当前目录)")
        p.add_argument("--target-path", dest="target_path",
                       help="dbt target 目录(默认: DBT_TARGET_PATH → dbt_project.yml 的 target-path → target)")
        return p

    p = common(sub.add_parser("init", help="生成 metriclens.yml 模板"))
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    p = common(sub.add_parser("graph", help="构建字段级血缘图"))
    p.add_argument("--allow-partial", action="store_true", help="存在解析失败的列时仍以 0 退出")
    p.set_defaults(fn=cmd_graph)
    p = common(sub.add_parser("trace", help="回溯 S/F/E 三元组"))
    p.add_argument("target", help="model.column")
    p.set_defaults(fn=cmd_trace)
    p = common(sub.add_parser("synth", help="双通道口径合成(LLM)"))
    p.add_argument("--only", help="只重跑一个指标 key(从 active 批次补齐其余)")
    p.set_defaults(fn=cmd_synth)
    p = common(sub.add_parser("drift", help="口径漂移检测"))
    p.add_argument("--strict", action="store_true", help="high 级漂移非零退出")
    p.add_argument("--dry-run", action="store_true", help="只对比不落盘")
    p.set_defaults(fn=cmd_drift)
    common(sub.add_parser("govern", help="指纹扫描 + LLM 仲裁 → 治理报告")).set_defaults(fn=cmd_govern)
    p = common(sub.add_parser("report", help="口径卡导出 HTML"))
    p.add_argument("-o", "--output", help="输出文件(默认 .metriclens/caliber_report.html)")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
