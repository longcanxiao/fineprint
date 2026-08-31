#!/usr/bin/env python3
"""口径知识库的批次寻址:runs/<run_id>/ 目录 + active_run 指针。

发布原子性契约:整批卡全部落盘后才切换指针;消费端只读 active 批次,
线上任何时刻都是一个完整一致的批次,不存在半新半旧。
"""
import json
import shutil
from pathlib import Path

# 口径卡批次的对外契约版本(0.9 冻结):卡片 JSON 与 index.json 都盖章。
# report/看板/公开 API 消费的是这份 schema;破坏性变更须递增并在 CHANGELOG 公告。
CARD_SCHEMA_VERSION = 1


class CaliberStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.runs = self.root / "runs"
        self.pointer = self.root / "active_run"

    def active_run_id(self) -> str | None:
        if not self.pointer.exists():
            return None
        try:
            return json.loads(self.pointer.read_text(encoding="utf-8"))["run_id"]
        except Exception:
            return None

    def active_dir(self) -> Path | None:
        rid = self.active_run_id()
        d = self.runs / rid if rid else None
        return d if d and d.is_dir() else None

    def run_dir(self, run_id: str) -> Path:
        d = self.runs / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def activate(self, run_id: str, meta: dict | None = None):
        self.runs.mkdir(parents=True, exist_ok=True)
        tmp = self.pointer.with_suffix(".tmp")
        tmp.write_text(json.dumps({"run_id": run_id, **(meta or {})}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.pointer)

    def prune(self, keep: int = 3, protect: str | None = None):
        if not self.runs.is_dir():
            return
        runs_sorted = sorted((d for d in self.runs.iterdir() if d.is_dir()),
                             key=lambda d: d.stat().st_mtime, reverse=True)
        for d in runs_sorted[keep:]:
            if d.name != protect:
                shutil.rmtree(d, ignore_errors=True)

    def card(self, key: str) -> dict | None:
        d = self.active_dir()
        f = (d / f"{key}.json") if d else None
        return json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else None

    def index(self) -> dict | None:
        d = self.active_dir()
        f = (d / "index.json") if d else None
        return json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else None
