#!/usr/bin/env python3
"""指纹扫描冒烟:T8 靶向对(交易域/售后域退款金额)必须被全列扫描自动发现。"""
import sys

from benchmark.paths import GRAPH, PROJECT_DIR
from metriclens.config import MLConfig
from metriclens.governance import scan
from metriclens.trace import load_graph

T8_PAIR = {"dm_trade_stats_1d.refund_amt", "dm_after_sale_stats_1d.refund_amt_total"}


def main():
    cfg = MLConfig.load(PROJECT_DIR)
    r = scan(load_graph(GRAPH), cfg)
    found = any({p["a"], p["b"]} == T8_PAIR for p in r["duplicates"])
    print(f"指纹扫描: A 档 {len(r['duplicates'])} 对 / B 档 {len(r['candidates'])} 对; "
          f"T8 靶向对自动发现: {'✓' if found else '✗'}")
    sys.exit(0 if found else 1)


if __name__ == "__main__":
    main()
