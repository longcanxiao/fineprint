#!/usr/bin/env python3
"""LLM 客户端(OpenAI 兼容):温度 0、JSON 输出、结构校验、重试、内容寻址缓存。

配置只走环境变量(或项目根 .env),密钥永不进配置文件:
  METRICLENS_LLM_BASE_URL      默认 https://api.openai.com/v1
  METRICLENS_LLM_API_KEY       必填(fallback: OPENAI_API_KEY)
  METRICLENS_LLM_MODEL         必填,如 deepseek-chat / gpt-4.1-mini
  METRICLENS_LLM_FAST_MODEL    可选,逐跳抽取用(默认 = MODEL)
  METRICLENS_LLM_QUALITY_MODEL 可选,归并/业务口径用(默认 = MODEL)

推理型模型注意:思维链计入 max_tokens——content 为空或 JSON 被截断且
finish_reason=length 时自动扩容重试(温度 0 下同预算必然复现)。
"""
import hashlib
import json
import os
import random
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

import requests

PROMPT_VER = "v3"
_CACHE_DIR: Path | None = None
# 同 key 并发去重:首跑全 miss 时,多个指标途经同一模型会同时发起相同请求,
# 各拿到不同回答导致下游 prompt 分叉、缓存键漂移(温度 0 也不保证逐字节一致)
_INFLIGHT: dict[str, threading.Lock] = {}
_INFLIGHT_GUARD = threading.Lock()
# 全局并发上限:synth 外层指标池 × 内层逐跳池可叠出几十路并发,统一在请求处限流
_SEM: threading.BoundedSemaphore | None = None
_SEM_GUARD = threading.Lock()


class FatalLLMError(RuntimeError):
    """不可重试的调用错误(4xx 凭据/模型名/请求体类):立即失败,不烧重试预算。"""


def _sem() -> threading.BoundedSemaphore:
    global _SEM
    with _SEM_GUARD:
        if _SEM is None:
            n = max(1, int(os.environ.get("METRICLENS_LLM_CONCURRENCY", "8")))
            _SEM = threading.BoundedSemaphore(n)
    return _SEM


def _backoff(attempt: int, retry_after: str | None) -> float:
    """指数退避 + 抖动;服务端给出 Retry-After 时优先遵循。"""
    if retry_after:
        try:
            return min(float(retry_after), 120.0)
        except ValueError:
            pass
    return min(2 ** attempt + random.uniform(0, 1), 60.0)


def set_cache_dir(p: Path | None):
    global _CACHE_DIR
    _CACHE_DIR = Path(p) if p else None


def load_dotenv(project_dir: Path):
    """项目根 .env 里的 METRICLENS_*/OPENAI_* 变量补进环境(不覆盖已有)。"""
    f = Path(project_dir) / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if (k.startswith("METRICLENS_") or k.startswith("OPENAI_")) and k not in os.environ:
            os.environ[k] = v.strip()


@lru_cache(maxsize=1)
def settings() -> dict:
    key = os.environ.get("METRICLENS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise KeyError(
            "缺少 LLM 凭据:请设置 METRICLENS_LLM_API_KEY(或 OPENAI_API_KEY),"
            "可放在被分析项目根目录的 .env 中")
    model = os.environ.get("METRICLENS_LLM_MODEL")
    if not model:
        raise KeyError("缺少 METRICLENS_LLM_MODEL(任意 OpenAI 兼容模型名)")
    base = (os.environ.get("METRICLENS_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    return {
        "api_key": key, "base_url": base, "model": model,
        "fast_model": os.environ.get("METRICLENS_LLM_FAST_MODEL", model),
        "quality_model": os.environ.get("METRICLENS_LLM_QUALITY_MODEL", model),
    }


def fast_model() -> str:
    return settings()["fast_model"]


def quality_model() -> str:
    return settings()["quality_model"]


def chat_json(system: str, user: str, max_tokens: int = 4000, use_cache: bool = True,
              model: str | None = None, validator=None) -> dict:
    cfg = settings()
    model = model or cfg["model"]
    if use_cache and _CACHE_DIR is not None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(f"{PROMPT_VER}|{model}|{system}|{user}".encode()).hexdigest()
        cf = _CACHE_DIR / f"{key}.json"
        with _INFLIGHT_GUARD:
            lock = _INFLIGHT.setdefault(key, threading.Lock())
        with lock:                            # 同 key 串行:后到者等首个完成后直接命中
            if cf.exists():
                try:
                    obj = json.loads(cf.read_text())
                    if validator:
                        validator(obj)
                    return obj
                except Exception:
                    cf.unlink(missing_ok=True)   # 缓存损坏或结构不合规:作废重取
            return _request(cfg, model, system, user, max_tokens, validator, cf)
    return _request(cfg, model, system, user, max_tokens, validator, None)


def _request(cfg: dict, model: str, system: str, user: str, max_tokens: int,
             validator, cf: Path | None) -> dict:
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
        retry_after = None
        try:
            with _sem():
                r = requests.post(f"{cfg['base_url']}/chat/completions", timeout=180,
                                  headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=payload)
            if r.status_code in (408, 429) or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            if 400 <= r.status_code < 500:
                # 凭据/模型名/请求体错误重试不会好转,速死并给出可行动信息
                raise FatalLLMError(f"HTTP {r.status_code}(不可重试,请检查 API key/模型名/请求): {r.text[:300]}")
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
                if ch.get("finish_reason") == "length":
                    budget = min(budget * 2, 16000)
                raise
            if validator:
                validator(obj)
            if cf is not None:
                # 进程内同 key 已由 _INFLIGHT 串行;tmp 名加随机后缀防跨进程互踩
                tmp = cf.parent / f"{cf.name}.{uuid.uuid4().hex[:6]}.tmp"
                tmp.write_text(json.dumps(obj, ensure_ascii=False))
                tmp.replace(cf)
            return obj
        except FatalLLMError:
            raise
        except Exception as e:
            last_err = e
            time.sleep(_backoff(attempt, retry_after))
    raise RuntimeError(f"LLM 调用失败(重试 8 次): {last_err}")
