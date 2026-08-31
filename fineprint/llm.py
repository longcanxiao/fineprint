#!/usr/bin/env python3
"""LLM 客户端(OpenAI 兼容):温度 0、JSON 输出、结构校验、重试、内容寻址缓存。

配置只走环境变量(或项目根 .env),密钥永不进配置文件:
  FINEPRINT_LLM_BASE_URL      默认 https://api.openai.com/v1
  FINEPRINT_LLM_API_KEY       必填(fallback: OPENAI_API_KEY)
  FINEPRINT_LLM_MODEL         必填,如 deepseek-chat / gpt-4.1-mini
  FINEPRINT_LLM_FAST_MODEL    可选,逐跳抽取用(默认 = MODEL)
  FINEPRINT_LLM_QUALITY_MODEL 可选,归并/业务口径用(默认 = MODEL)
  FINEPRINT_LLM_TIMEOUT       可选,单次请求超时秒数(默认 180)
  FINEPRINT_LLM_RETRIES       可选,最大重试轮数(默认 8)

重试不再沉默:每次退避经 on_retry 钩子上报(默认打一行 stderr——错误类型/
第几轮/等多久),长任务"几分钟没动静"的最大来源就是这里。

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

import warnings

# 兜底一道:Python API 直接 import fineprint.llm 也不放 urllib3 告警进来
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
import requests  # noqa: E402

from fineprint.i18n import t  # noqa: E402

PROMPT_VER = "v4"


def _print_retry(info: dict):
    print(t(f"  ⟳ LLM 重试 {info['attempt']}/{info['max']}({info['error']}),"
            f"等待 {info['wait']:.1f}s",
            f"  ⟳ LLM retry {info['attempt']}/{info['max']} ({info['error']}), "
            f"waiting {info['wait']:.1f}s"), file=__import__("sys").stderr, flush=True)


# 重试上报钩子:默认一行 stderr;synth --json 模式替换为 JSONL 事件发射器
on_retry = _print_retry


def _timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("FINEPRINT_LLM_TIMEOUT", "180")))
    except ValueError:
        return 180.0


def _retries() -> int:
    try:
        return max(1, int(os.environ.get("FINEPRINT_LLM_RETRIES", "8")))
    except ValueError:
        return 8
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
            n = max(1, int(os.environ.get("FINEPRINT_LLM_CONCURRENCY", "8")))
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
    """项目根 .env 里的 FINEPRINT_*/OPENAI_* 变量补进环境(不覆盖已有)。
    旧 METRICLENS_* 键(≤0.8.3)不再生效——静默失效最伤人,点名提示改键。"""
    f = Path(project_dir) / ".env"
    if not f.exists():
        return
    legacy = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if (k.startswith("FINEPRINT_") or k.startswith("OPENAI_")) and k not in os.environ:
            os.environ[k] = v.strip()
        elif k.startswith("METRICLENS_"):
            legacy.append(k)
    if legacy:
        import sys
        print(t(f"⚠ {f} 含旧 METRICLENS_* 变量({len(legacy)} 个:{', '.join(legacy[:4])}"
                f"{'…' if len(legacy) > 4 else ''}):0.8.4 起改名 FINEPRINT_*,"
                f"旧键不再生效,请更新键名(值不用变)",
                f"⚠ {f} contains legacy METRICLENS_* variables ({len(legacy)}: "
                f"{', '.join(legacy[:4])}{'…' if len(legacy) > 4 else ''}): renamed to "
                f"FINEPRINT_* in 0.8.4 — the old keys no longer take effect; "
                f"update the key names (values unchanged)"), file=sys.stderr)


def _legacy_env_hint() -> str:
    """进程环境里带着旧 METRICLENS_LLM_*(shell export 的老配置):报错顺路指路。"""
    if not any(k.startswith("METRICLENS_LLM_") for k in os.environ):
        return ""
    return t("\n(检测到旧 METRICLENS_LLM_* 环境变量:0.8.4 起改名 FINEPRINT_LLM_*,"
             "旧名不再生效,改键名即可,值不用变)",
             "\n(legacy METRICLENS_LLM_* environment variables detected: renamed to "
             "FINEPRINT_LLM_* in 0.8.4 — the old names no longer take effect; "
             "rename the keys, values unchanged)")


@lru_cache(maxsize=1)
def settings() -> dict:
    # 缺什么一次列全:这也是 synth 开工前预检的报错口径,别让用户逐个变量试错
    key = os.environ.get("FINEPRINT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("FINEPRINT_LLM_MODEL")
    missing = []
    if not key:
        missing.append(t("FINEPRINT_LLM_API_KEY(或 OPENAI_API_KEY)= 凭据",
                         "FINEPRINT_LLM_API_KEY (or OPENAI_API_KEY) = credentials"))
    if not model:
        missing.append(t("FINEPRINT_LLM_MODEL = 任意 OpenAI 兼容模型名",
                         "FINEPRINT_LLM_MODEL = any OpenAI-compatible model name"))
    if missing:
        raise KeyError(t(
            "缺少 LLM 配置:\n  - " + "\n  - ".join(missing) +
            "\n可放在被分析项目根目录的 .env 中(FINEPRINT_LLM_BASE_URL 缺省 "
            "https://api.openai.com/v1)",
            "missing LLM configuration:\n  - " + "\n  - ".join(missing) +
            "\nset them e.g. in a .env file at the analyzed project's root "
            "(FINEPRINT_LLM_BASE_URL defaults to https://api.openai.com/v1)")
            + _legacy_env_hint())
    base = (os.environ.get("FINEPRINT_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    return {
        "api_key": key, "base_url": base, "model": model,
        "fast_model": os.environ.get("FINEPRINT_LLM_FAST_MODEL", model),
        "quality_model": os.environ.get("FINEPRINT_LLM_QUALITY_MODEL", model),
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
        key = hashlib.md5(
            f"{PROMPT_VER}|{cfg['base_url']}|{model}|{max_tokens}|{system}|{user}".encode()).hexdigest()
        cf = _CACHE_DIR / f"{key}.json"
        with _INFLIGHT_GUARD:
            lock = _INFLIGHT.setdefault(key, threading.Lock())
        with lock:                            # 同 key 串行:后到者等首个完成后直接命中
            if cf.exists():
                try:
                    obj = json.loads(cf.read_text(encoding="utf-8"))
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
    max_attempts = _retries()
    for attempt in range(max_attempts):
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
                r = requests.post(f"{cfg['base_url']}/chat/completions", timeout=_timeout(),
                                  headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=payload)
            if r.status_code in (408, 429) or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            if 400 <= r.status_code < 500:
                # 凭据/模型名/请求体错误重试不会好转,速死并给出可行动信息
                raise FatalLLMError(t(
                    f"HTTP {r.status_code}(不可重试,请检查 API key/模型名/请求): {r.text[:300]}",
                    f"HTTP {r.status_code} (not retryable — check API key/model name/request): {r.text[:300]}"))
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
                tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
                tmp.replace(cf)
            return obj
        except FatalLLMError:
            raise
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:   # 最后一轮失败直接抛出,不再空等退避
                wait = _backoff(attempt, retry_after)
                try:
                    on_retry({"attempt": attempt + 1, "max": max_attempts, "model": model,
                              "error": f"{type(e).__name__}: {str(e)[:120]}", "wait": wait})
                except Exception:
                    pass                     # 上报钩子绝不反噬调用
                time.sleep(wait)
    raise RuntimeError(t(f"LLM 调用失败(重试 {max_attempts} 次): {last_err}",
                         f"LLM call failed after {max_attempts} attempts: {last_err}"))
