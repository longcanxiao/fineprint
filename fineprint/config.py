#!/usr/bin/env python3
"""fineprint.yml 配置:指标清单、语言、业务词典、治理参数。

放在 dbt 项目根目录。密钥永不进配置文件——LLM 凭据只走环境变量(见 llm.py)。
"""
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fineprint.i18n import t

# key 直接用作批次目录内的文件名:限定字符集,杜绝路径分隔符与 ../ 逃逸
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# target 两段 model.column;短名跨包歧义时用三段 package.model.column 消歧
TARGET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
RESERVED_KEYS = {"index", "active_run"}


def _internal_packages_of(raw: dict) -> tuple:
    v = raw.get("internal_packages")
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ValueError(t(f"internal_packages 须为字符串列表(dbt 包名),实得 {v!r}",
                           f"internal_packages must be a list of strings (dbt package names), "
                           f"got {v!r}"))
    return tuple(v)


def read_internal_packages(project_dir) -> tuple:
    """fineprint.yml 顶层 internal_packages:按一方代码解析的额外 dbt 包名单。

    独立于 MLConfig.load 的轻量读取器——`fineprint graph` 不要求完整配置
    (可以没有 metrics),但第三方包的数据源边界判定必须在建图时就生效。"""
    f = Path(project_dir) / "fineprint.yml"
    if not f.exists():
        return ()
    raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return _internal_packages_of(raw) if isinstance(raw, dict) else ()

EXAMPLE = """\
# FinePrint 配置(放在 dbt 项目根目录)
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
# internal_packages: [shared_models]  # 可选:按一方代码解析的 dbt 包;
#                     其余第三方包(Fivetran/dbt_utils 等)的模型一律按数据源边界
#                     处理——不解析其 SQL 与口径,血缘在其物化表处截止
governance:
  scan_layers: []       # 指纹重复扫描的分层白名单;空 = 全部模型
  base_suffixes: [_total, _14d, _1d, _7d, _30d]   # 判定"同基名"时剥离的后缀
  skip_columns: []      # 扫描跳过的列名
  skip_suffixes: [_id, _date, _time, _key]
  max_llm_pairs: 40     # B 档 LLM 仲裁的候选上限(超出截断并提示,控制成本)
"""

EXAMPLE_EN = """\
# FinePrint config (place at the dbt project root)
language: en            # caliber card language: zh | en
metrics:                # dashboard metrics to synthesize cards for (model.column)
  - key: revenue
    title: Revenue
    target: fct_orders_daily.revenue
  # - key: refund_rate
  #   title: Refund rate
  #   target: mart_refunds.refund_rate
  #   extra_targets: [mart_refunds.channel]   # optional: related columns traced together
  #   query_filter: "channel = 'live'"        # optional: query-layer filter note
lexicon: {}             # optional: business glossary (term → meaning) cited by business calibers
# internal_packages: [shared_models]  # optional: extra dbt packages parsed as first-party code;
#                     all other third-party packages (Fivetran/dbt_utils/…) are treated as
#                     data-source boundaries — their SQL is not parsed, lineage stops at
#                     their materialized tables
governance:
  scan_layers: []       # layer allowlist for the duplicate fingerprint scan; empty = all models
  base_suffixes: [_total, _14d, _1d, _7d, _30d]   # suffixes stripped for "same base name"
  skip_columns: []      # column names skipped by the scan
  skip_suffixes: [_id, _date, _time, _key]
  max_llm_pairs: 40     # cap on LLM-arbitrated candidate pairs (truncates with a note)
"""


def example_yml() -> str:
    return t(EXAMPLE, EXAMPLE_EN)


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
    internal_packages: tuple = ()
    scan_layers: list = field(default_factory=list)
    base_suffixes: list = field(default_factory=lambda: ["_total", "_14d", "_1d", "_7d", "_30d"])
    skip_columns: list = field(default_factory=list)
    skip_suffixes: list = field(default_factory=lambda: ["_id", "_date", "_time", "_key"])
    max_llm_pairs: int = 40
    path: Path | None = None

    @classmethod
    def load(cls, project_dir: Path) -> "MLConfig":
        f = Path(project_dir) / "fineprint.yml"
        if not f.exists():
            legacy = ""
            if (Path(project_dir) / "metriclens.yml").exists():
                # 0.8.4 品牌统一改名:老项目升级后最常见的第一脚,指路而非让人猜
                legacy = t("\n检测到旧配置 metriclens.yml:0.8.4 起统一改名,执行 "
                           "mv metriclens.yml fineprint.yml 即可(内容格式未变)",
                           "\nfound legacy metriclens.yml: renamed in 0.8.4 — run "
                           "mv metriclens.yml fineprint.yml (the format is unchanged)")
            raise FileNotFoundError(t(
                f"未找到 {f}\n请先执行 fineprint init 生成配置,或手工创建(模板见 README){legacy}",
                f"{f} not found\nrun fineprint init to generate the config, "
                f"or create it by hand (template in README){legacy}"))
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(t(f"{f} 顶层须为映射(language/metrics/…),实得 {type(raw).__name__}",
                               f"top level of {f} must be a mapping (language/metrics/…), "
                               f"got {type(raw).__name__}"))
        gov = raw.get("governance") or {}
        if not isinstance(gov, dict):
            raise ValueError(t(f"governance 须为映射,实得 {type(gov).__name__}",
                               f"governance must be a mapping, got {type(gov).__name__}"))
        metrics, seen_keys = [], set()
        for m in raw.get("metrics") or []:
            if not isinstance(m, dict) or not m.get("key") or not m.get("target"):
                raise ValueError(t(f"metrics 条目缺少 key/target: {m}",
                                   f"metrics entry missing key/target: {m}"))
            key = str(m["key"])
            if not KEY_RE.match(key):
                raise ValueError(t(f"metric key 非法: {key!r}(须匹配 {KEY_RE.pattern},不得含路径分隔符)",
                                   f"invalid metric key: {key!r} (must match {KEY_RE.pattern}; "
                                   f"path separators not allowed)"))
            if key.lower() in RESERVED_KEYS:
                raise ValueError(t(f"metric key 为保留名: {key!r}(保留: {sorted(RESERVED_KEYS)})",
                                   f"metric key is a reserved name: {key!r} "
                                   f"(reserved: {sorted(RESERVED_KEYS)})"))
            # 大小写不敏感文件系统(macOS/Windows 默认)上 GMV.json 与 gmv.json 同文件,
            # NFC + casefold 判重杜绝跨平台互相覆盖
            folded = unicodedata.normalize("NFC", key).casefold()
            if folded in seen_keys:
                raise ValueError(t(f"metric key 重复: {key!r}(大小写不敏感文件系统上同批发布会互相覆盖)",
                                   f"duplicate metric key: {key!r} (cards in one batch would "
                                   f"overwrite each other on case-insensitive filesystems)"))
            seen_keys.add(folded)
            targets = [m["target"], *(m.get("extra_targets") or [])]
            for tgt in targets:
                if not isinstance(tgt, str) or not TARGET_RE.match(tgt):
                    raise ValueError(t(f"metric {key!r} 的 target/extra_targets 须为 'model.column'"
                                       f"(或跨包重名时 'package.model.column'),实得 {tgt!r}",
                                       f"metric {key!r} target/extra_targets must be 'model.column' "
                                       f"(or 'package.model.column' when short names collide "
                                       f"across packages), got {tgt!r}"))
            qf = m.get("query_filter")
            if qf is not None and not isinstance(qf, str):
                raise ValueError(t(f"metric {key!r} 的 query_filter 须为字符串,实得 {type(qf).__name__}",
                                   f"metric {key!r} query_filter must be a string, "
                                   f"got {type(qf).__name__}"))
            metrics.append(MetricDef(
                key=key, title=m.get("title", key), target=m["target"],
                extra_targets=list(m.get("extra_targets") or []), query_filter=qf))
        if not metrics:
            raise ValueError(t(f"{f} 的 metrics 为空:至少配置一个 model.column 目标",
                               f"metrics in {f} is empty: configure at least one "
                               f"model.column target"))

        def _strlist(field_name: str, v, default: list) -> list:
            v = v if v is not None else default
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(t(f"governance.{field_name} 须为字符串列表,实得 {v!r}",
                                   f"governance.{field_name} must be a list of strings, got {v!r}"))
            return v

        pairs = gov.get("max_llm_pairs", cls().max_llm_pairs)
        if not isinstance(pairs, int) or isinstance(pairs, bool) or pairs < 0:
            raise ValueError(t(f"governance.max_llm_pairs 须为非负整数,实得 {pairs!r}",
                               f"governance.max_llm_pairs must be a non-negative integer, "
                               f"got {pairs!r}"))
        lex = raw.get("lexicon") or {}
        if not isinstance(lex, dict):
            raise ValueError(t(f"lexicon 须为映射(术语 → 解释),实得 {type(lex).__name__}",
                               f"lexicon must be a mapping (term → meaning), "
                               f"got {type(lex).__name__}"))
        cfg = cls(
            language=raw.get("language", "zh"), metrics=metrics,
            lexicon=lex,
            internal_packages=_internal_packages_of(raw),
            scan_layers=_strlist("scan_layers", gov.get("scan_layers"), []),
            base_suffixes=_strlist("base_suffixes", gov.get("base_suffixes"), cls().base_suffixes),
            skip_columns=_strlist("skip_columns", gov.get("skip_columns"), []),
            skip_suffixes=_strlist("skip_suffixes", gov.get("skip_suffixes"), cls().skip_suffixes),
            max_llm_pairs=pairs,
            path=f)
        if cfg.language not in ("zh", "en"):
            raise ValueError(t(f"language 须为 zh|en,实得 {cfg.language!r}",
                               f"language must be zh|en, got {cfg.language!r}"))
        from fineprint.i18n import set_lang
        set_lang(cfg.language)   # 配置是语言的权威来源(env FINEPRINT_LANG 仍可覆盖)
        return cfg

    def metric(self, key: str) -> MetricDef | None:
        return next((m for m in self.metrics if m.key == key), None)
