#!/usr/bin/env python3
"""benchmark 数仓(14 道口径陷阱)的固定路径。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = ROOT / "warehouse" / "dbt_project"
WORKSPACE = PROJECT_DIR / ".metriclens"


def _graph() -> Path:
    """METRICLENS_GRAPH 覆盖(rebuild 在 build 位置产图,验证通过才切换)。"""
    return Path(os.environ.get("METRICLENS_GRAPH") or WORKSPACE / "graph.json")


GRAPH = _graph()
