#!/usr/bin/env python3
"""DeepSeek LLM 客户端(OpenAI 兼容):温度 0、JSON 输出、结构校验、重试、原子缓存。

配置延迟加载:import 本模块不读取任何密钥;首个 LLM 调用时才解析 CALIBER_ENV_FILE。
生产映射:任意 OpenAI 兼容端点(vLLM 私有化同理);切换 Claude API 只需替换本文件。
"""
import hashlib
import json
import os
import time
from functools import lru_cache
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent / "store" / ".llm_cache"
PROMPT_VER = "v2"
_DEFAULT_ENV = "/Users/qiyi/projects/agent-dev/.env.shared"


@lru_cache(maxsize=1)
def settings() -> dict:
    """惰性解析 LLM 配置;干净环境 import 不失败,调用时才要求配置存在。"""
    env_file = Path(os.environ.get("CALIBER_ENV_FILE", _DEFAULT_ENV))
    if not env_file.exists():
        raise FileNotFoundError(
            f"LLM 配置文件不存在: {env_file}\n"
            f"请设置环境变量 CALIBER_ENV_FILE 指向你的配置(模板见项目根 .env.example)")
    cfg = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    if "DEEPSEEK_API_KEY" not in cfg:
        raise KeyError(f"{env_file} 缺少 DEEPSEEK_API_KEY(模板见项目根 .env.example)")
    base = cfg.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = cfg.get("DEEPSEEK_MODEL", "deepseek-chat")
    return {
        "api_key": cfg["DEEPSEEK_API_KEY"], "base_url": base, "model": model,
        "fast_model": cfg.get("DEEPSEEK_FAST_MODEL", model),
        "quality_model": cfg.get("DEEPSEEK_QUALITY_MODEL", model),
    }


def fast_model() -> str:
    return settings()["fast_model"]


def quality_model() -> str:
    return settings()["quality_model"]


def chat_json(system: str, user: str, max_tokens: int = 4000, use_cache: bool = True,
              model: str | None = None, validator=None) -> dict:
    """单轮对话,强制 JSON 输出;结构校验失败按解析失败重试;按输入哈希原子缓存。

    注意:推理模型思维链计入 max_tokens——content 为空且 finish=length 时自动扩容重试。
    """
    cfg = settings()
    model = model or cfg["model"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{PROMPT_VER}|{model}|{system}|{user}".encode()).hexdigest()
    cf = CACHE_DIR / f"{key}.json"
    if use_cache and cf.exists():
        try:
            obj = json.loads(cf.read_text())
            if validator:
                validator(obj)
            return obj
        except Exception:
            cf.unlink(missing_ok=True)   # 缓存损坏或结构不合规:作废重取
    last_err = None
    budget = max_tokens
    for attempt in range(8):
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": budget,
            "response_format": {"type": "json_object"},
        }
        try:
            r = requests.post(f"{cfg['base_url']}/chat/completions", timeout=180,
                              headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=payload)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            ch = r.json()["choices"][0]
            content = ch["message"]["content"] or ""
            if not content.strip():
                if ch.get("finish_reason") == "length":
                    budget = min(budget * 2, 16000)
                    raise RuntimeError(f"empty content (finish=length), retry with max_tokens={budget}")
                raise RuntimeError("empty content")
            try:
                obj = json.loads(content)
            except json.JSONDecodeError:
                # content 非空但 JSON 截断:推理模型思维链吃掉预算后 content 被 length 掐断,
                # 温度 0 重试同预算必然复现——必须扩容
                if ch.get("finish_reason") == "length":
                    budget = min(budget * 2, 16000)
                raise
            if validator:
                validator(obj)
            tmp = cf.with_suffix(".tmp")
            tmp.write_text(json.dumps(obj, ensure_ascii=False))
            tmp.replace(cf)
            return obj
        except Exception as e:
            last_err = e
            # 429/5xx/超时等服务端抖动退避更久,避免把预算翻倍机会烧在限流窗口里
            transient = isinstance(e, (requests.RequestException,)) or "HTTP" in str(e)
            time.sleep((5 if transient else 2) * (attempt + 1))
    raise RuntimeError(f"LLM 调用失败(重试 8 次): {last_err}")


if __name__ == "__main__":
    out = chat_json("你是 JSON 回声器,只输出 JSON。", '返回 {"ok": true}', use_cache=False)
    print("smoke:", out, "| endpoint:", settings()["base_url"], "| model:", settings()["model"])
