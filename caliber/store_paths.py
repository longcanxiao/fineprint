#!/usr/bin/env python3
"""口径知识库的批次寻址:runs/<run_id>/ 目录 + active_run 指针(轻量,无 LLM 依赖)。"""
import json
from pathlib import Path

STORE = Path(__file__).resolve().parent / "store"
RUNS = STORE / "runs"
POINTER = STORE / "active_run"


def active_run_id() -> str | None:
    if not POINTER.exists():
        return None
    try:
        return json.loads(POINTER.read_text())["run_id"]
    except Exception:
        return None


def active_dir() -> Path | None:
    rid = active_run_id()
    d = RUNS / rid if rid else None
    return d if d and d.is_dir() else None


def activate(run_id: str, meta: dict | None = None):
    """原子切换 active 指针:整批卡全部落盘后才调用。"""
    RUNS.mkdir(parents=True, exist_ok=True)
    tmp = POINTER.with_suffix(".tmp")
    tmp.write_text(json.dumps({"run_id": run_id, **(meta or {})}, ensure_ascii=False))
    tmp.replace(POINTER)
