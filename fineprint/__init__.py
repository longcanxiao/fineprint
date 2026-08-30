"""FinePrint — metric caliber synthesis for dbt projects.

Reverse-engineers the business & technical definition ("caliber") of every
dashboard metric from your existing multi-layer SQL: deterministic column-level
lineage × LLM per-model reading, cross-validated, evidence-bound, and published
as consumer-facing caliber cards.

Distributed on PyPI as ``fineprint`` — read the fine print of your metrics;
a decompiler for your dashboards.
"""
import warnings as _warnings

# urllib3 在 LibreSSL 环境(macOS 系统 Python)import 即告警,与用户操作无关;
# 在包导入期(console script 的第一步)就位,先于任何 requests/urllib3 导入。
# 按消息精确静默,不整类屏蔽;若宿主环境在解释器启动期已抢先 import 过 urllib3,
# 告警已发出,进程内无从追回——那是环境级问题,此处覆盖到能覆盖的最早点。
_warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

try:
    from importlib.metadata import version as _pkg_version
    # 版本单一事实源 = 包元数据(pyproject);硬编码副本曾在 0.8.5 后失同步
    __version__ = _pkg_version("fineprint")
except Exception:                       # 未安装(纯源码路径运行)时的诚实占位
    __version__ = "0+unknown"
