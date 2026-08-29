#!/usr/bin/env python3
"""真实 dbt 项目探针:量确定性组合器在外部项目上的覆盖率(0.8 赛马裁决数据)。

输入:任意 dbt artifacts(target/manifest.json + target/catalog.json,须含
compiled_code——dbt docs generate 的产物即满足)。探针自包含:从 manifest 把
compiled_code 物化成 compiled_path 文件,建血缘图,然后对全部模型列逐一跑
组合器,输出 status 分布与 unsupported 原因直方图。零 LLM、零数据库连接。

用法:
  python benchmark/probe_real_project.py --artifacts DIR [--all-packages]
      [--out report.json] [--limit N]
  DIR 下须有 target/manifest.json 与 target/catalog.json。
  --all-packages 把 manifest 里所有模型包声明为一方包(探测包类项目时用:
  docs 站点的 root 常是集成测试壳,真实模型都在依赖包里)。
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metriclens.lineage import build_graph  # noqa: E402
from metriclens.project import DbtProject  # noqa: E402
from metriclens.render import _Composer  # noqa: E402

# unsupported/ambiguous 原因归桶:前缀匹配,可枚举可修的清单
REASON_BUCKETS = [
    ("UNION", "union_divergent"),
    ("标量子查询", "scalar_subquery"),
    ("作用域投影中找不到列", "missing_projection"),
    ("缺少限定名", "unqualified_multi_source"),
    ("在作用域中无来源", "unknown_alias"),
    ("表达式链存在环", "cycle_self_ref"),
    ("展开深度超限", "depth_cap"),
    ("命名子表达式规模超限", "defs_cap"),
    ("qualify 失败", "qualify_failed"),
    ("非 SELECT 结构", "not_select"),
    ("多个 database 中同名", "ambiguous_two_part"),
    ("internal:", "internal_error"),
    ("round-trip", "round_trip"),
]


def bucket(reason: str) -> str:
    for pat, name in REASON_BUCKETS:
        if pat in reason:
            return name
    return "other"


def materialize_compiled(project_dir: Path, manifest: dict) -> int:
    n = 0
    for node in manifest["nodes"].values():
        if node.get("resource_type") != "model":
            continue
        cc, cp = node.get("compiled_code"), node.get("compiled_path")
        if not cc or not cp:
            continue
        f = project_dir / cp
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(cc)
        n += 1
    return n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True, help="含 target/manifest.json 的项目目录")
    ap.add_argument("--all-packages", action="store_true",
                    help="manifest 内所有模型包按一方包解析(包类项目探测)")
    ap.add_argument("--limit", type=int, default=0, help="最多组合多少列(0=全部)")
    ap.add_argument("--out", default="", help="JSON 报告输出路径")
    args = ap.parse_args(argv)

    pdir = Path(args.artifacts)
    manifest = json.loads((pdir / "target" / "manifest.json").read_text())
    wrote = materialize_compiled(pdir, manifest)
    internal = None
    if args.all_packages:
        internal = sorted({n.get("package_name") for n in manifest["nodes"].values()
                           if n.get("resource_type") == "model" and n.get("package_name")})
    proj = DbtProject(pdir, internal_packages=internal)
    print(f"adapter={proj.adapter_type}  物化编译文件 {wrote} 个"
          f"  internal_packages={'ALL(' + str(len(internal)) + ')' if internal else '默认(root)'}")

    t0 = time.time()
    graph = build_graph(proj)
    t_graph = time.time() - t0
    models = graph["models"]
    col_total = sum(len(m["columns"]) for m in models.values())
    col_lineage_err = sum(1 for m in models.values()
                          for c in m["columns"].values() if c.get("error"))
    print(f"建图 {t_graph:.1f}s: 模型 {len(models)}  列 {col_total}"
          f"  列级血缘错误 {col_lineage_err}")

    comp = _Composer(proj, graph)
    status_cnt, reason_cnt, note_cnt = Counter(), Counter(), Counter()
    by_pkg: dict = {}
    samples: dict = {}
    done = 0
    t1 = time.time()
    for uid, m in sorted(models.items()):
        for col in m["columns"]:
            if args.limit and done >= args.limit:
                break
            done += 1
            c = comp.compose_target(uid, col)
            status_cnt[c["status"]] += 1
            pkg = m.get("package") or "?"
            by_pkg.setdefault(pkg, Counter())[c["status"]] += 1
            for r in c["reasons"]:
                b = bucket(r)
                reason_cnt[b] += 1
                samples.setdefault(b, f"{uid}.{col}: {r[:140]}")
            for nt in c.get("notes") or []:
                note_cnt[bucket(nt)] += 1
    t_comp = time.time() - t1

    print(f"\n组合 {done} 列,耗时 {t_comp:.1f}s ({done / max(t_comp, 0.01):.0f} 列/s)")
    for k, v in status_cnt.most_common():
        print(f"  {k:<12} {v:>6}  ({v / max(done, 1):.1%})")
    if reason_cnt:
        print("\nunsupported/ambiguous 原因分布:")
        for k, v in reason_cnt.most_common():
            print(f"  {k:<24} {v:>6}   例: {samples.get(k, '')[:110]}")
    worst = sorted(by_pkg.items(),
                   key=lambda kv: -(kv[1].get("unsupported", 0) / max(sum(kv[1].values()), 1)))
    print("\n按包 unsupported 率(降序):")
    for pkg, cnt in worst:
        tot = sum(cnt.values())
        print(f"  {pkg:<28} {cnt.get('unsupported', 0):>5}/{tot:<6}"
              f" ({cnt.get('unsupported', 0) / max(tot, 1):.1%})")

    if args.out:
        report = {
            "artifacts": str(pdir), "adapter": proj.adapter_type,
            "models": len(models), "columns": col_total,
            "lineage_col_errors": col_lineage_err,
            "graph_seconds": round(t_graph, 1), "compose_seconds": round(t_comp, 1),
            "composed": done, "status": dict(status_cnt),
            "reasons": dict(reason_cnt), "reason_samples": samples,
            "notes": dict(note_cnt),
            "by_package": {k: dict(v) for k, v in by_pkg.items()},
        }
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"\n报告 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
