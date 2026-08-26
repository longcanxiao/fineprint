#!/usr/bin/env python3
"""血缘 CLI:python -m lineage.cli trace <model> <column> [--json]"""
import argparse
import json

from lineage.trace import load_graph, render, trace


def main():
    ap = argparse.ArgumentParser(prog="metriclens-lineage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    tp = sub.add_parser("trace", help="从模型列回溯至 ODS")
    tp.add_argument("model")
    tp.add_argument("column")
    tp.add_argument("--json", action="store_true")
    lp = sub.add_parser("list", help="列出模型与列")
    lp.add_argument("model", nargs="?")
    args = ap.parse_args()

    g = load_graph()
    if args.cmd == "list":
        if args.model:
            print("\n".join(g["models"][args.model]["columns"]))
        else:
            for n, m in g["models"].items():
                print(f"{m['layer']:>4}  {n}  ({len(m['columns'])} 列)")
        return
    t = trace(g, args.model, args.column)
    print(json.dumps(t, ensure_ascii=False, indent=1) if args.json else render(t))


if __name__ == "__main__":
    main()
