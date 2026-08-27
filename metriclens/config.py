#!/usr/bin/env python3
"""metriclens.yml 配置:指标清单、语言、业务词典、治理参数。

放在 dbt 项目根目录。密钥永不进配置文件——LLM 凭据只走环境变量(见 llm.py)。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# key 直接用作批次目录内的文件名:限定字符集,杜绝路径分隔符与 ../ 逃逸
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
RESERVED_KEYS = {"index", "active_run"}

EXAMPLE = """\
# MetricLens 配置(放在 dbt 项目根目录)
language: zh            # 口径卡语言: zh | en
metrics:                # 要合成口径卡的看板指标(model.column)
  - key: revenue
    title: 营收
    target: fct_orders_daily.revenue
  # - key: refund_rate
  #   title: 退款率
  #   target: mart_refunds.refund_rate
  #   extra_targets: [mart_refunds.channel]   # 可选:合并回溯的关联列
  #   query_filter: "channel = 'live'"        # 可选:取数时的查询层过滤说明
lexicon: {}             # 可选:业务词典(术语 → 解释),供业务口径生成引用
governance:
  scan_layers: []       # 指纹重复扫描的分层白名单;空 = 全部模型
  base_suffixes: [_total, _14d, _1d, _7d, _30d]   # 判定"同基名"时剥离的后缀
  skip_columns: []      # 扫描跳过的列名
  skip_suffixes: [_id, _date, _time, _key]
"""


@dataclass
class MetricDef:
    key: str
    title: str
    target: str
    extra_targets: list = field(default_factory=list)
    query_filter: str | None = None


@dataclass
class MLConfig:
    language: str = "zh"
    metrics: list = field(default_factory=list)
    lexicon: dict = field(default_factory=dict)
    scan_layers: list = field(default_factory=list)
    base_suffixes: list = field(default_factory=lambda: ["_total", "_14d", "_1d", "_7d", "_30d"])
    skip_columns: list = field(default_factory=list)
    skip_suffixes: list = field(default_factory=lambda: ["_id", "_date", "_time", "_key"])
    path: Path | None = None

    @classmethod
    def load(cls, project_dir: Path) -> "MLConfig":
        f = Path(project_dir) / "metriclens.yml"
        if not f.exists():
            raise FileNotFoundError(
                f"未找到 {f}\n请先执行 metriclens init 生成配置,或手工创建(模板见 README)")
        raw = yaml.safe_load(f.read_text()) or {}
        gov = raw.get("governance") or {}
        metrics, seen_keys = [], set()
        for m in raw.get("metrics") or []:
            if not m.get("key") or not m.get("target"):
                raise ValueError(f"metrics 条目缺少 key/target: {m}")
            key = str(m["key"])
            if not KEY_RE.match(key):
                raise ValueError(f"metric key 非法: {key!r}(须匹配 {KEY_RE.pattern},不得含路径分隔符)")
            if key.lower() in RESERVED_KEYS:
                raise ValueError(f"metric key 为保留名: {key!r}(保留: {sorted(RESERVED_KEYS)})")
            if key in seen_keys:
                raise ValueError(f"metric key 重复: {key!r}(同批发布会互相覆盖)")
            seen_keys.add(key)
            metrics.append(MetricDef(
                key=key, title=m.get("title", key), target=m["target"],
                extra_targets=m.get("extra_targets") or [], query_filter=m.get("query_filter")))
        if not metrics:
            raise ValueError(f"{f} 的 metrics 为空:至少配置一个 model.column 目标")
        cfg = cls(
            language=raw.get("language", "zh"), metrics=metrics,
            lexicon=raw.get("lexicon") or {},
            scan_layers=gov.get("scan_layers") or [],
            base_suffixes=gov.get("base_suffixes") or cls().base_suffixes,
            skip_columns=gov.get("skip_columns") or [],
            skip_suffixes=gov.get("skip_suffixes") or cls().skip_suffixes,
            path=f)
        if cfg.language not in ("zh", "en"):
            raise ValueError(f"language 须为 zh|en,实得 {cfg.language!r}")
        return cfg

    def metric(self, key: str) -> MetricDef | None:
        return next((m for m in self.metrics if m.key == key), None)
