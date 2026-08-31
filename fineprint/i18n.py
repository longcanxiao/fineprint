#!/usr/bin/env python3
"""CLI/展示层双语文案(zh|en)。卡片内容语言由 fineprint.yml 的 language 驱动
(提示词层早已双语);本模块把工具自身的输出(进度、错误、trace/报告骨架文案)
对齐到同一开关。

解析顺序(先到先得):
  1. FINEPRINT_LANG 环境变量(显式覆盖,zh|en)
  2. set_lang() —— 配置加载后由 cfg.language 注入;CLI 启动期会先窥探
     fineprint.yml 的 language 键(容错,读不到不报错)
  3. 系统 locale(LC_ALL/LC_MESSAGES/LANG 含 zh → zh)
  4. en —— 默认英文:首批用户是国际用户,无任何信号时不说中文
"""
import os
import re
from pathlib import Path

_LANG: str | None = None


def set_lang(v):
    """配置层注入(cfg.language);非法值忽略,不覆盖已有判定。"""
    global _LANG
    if v in ("zh", "en"):
        _LANG = v


def lang() -> str:
    env = os.environ.get("FINEPRINT_LANG")
    if env in ("zh", "en"):
        return env
    if _LANG:
        return _LANG
    for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(k) or ""
        if v.lower().startswith("zh"):
            return "zh"
        if v and v not in ("C", "POSIX"):
            break
    return "en"


def t(zh: str, en: str) -> str:
    return zh if lang() == "zh" else en


def peek_project_lang(project_dir) -> None:
    """CLI 启动期窥探 fineprint.yml 的 language 键(不做完整配置加载——
    配置本身可能就是报错对象,报错文案的语言不能依赖配置加载成功)。"""
    try:
        f = Path(project_dir) / "fineprint.yml"
        m = re.search(r"^language:\s*[\"']?(zh|en)\b", f.read_text(encoding="utf-8"), re.M)
        if m:
            set_lang(m.group(1))
    except Exception:
        pass
