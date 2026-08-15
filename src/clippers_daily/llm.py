from __future__ import annotations

import os
import time

from openai import OpenAI


def _provider_key(item: dict) -> str | None:
    key = os.getenv(item.get("api_key_env", "")) if item.get("api_key_env") else None
    path = item.get("api_key_file")
    if not key and path:
        try:
            key = open(path, encoding="utf-8").read().strip()
        except OSError:
            key = None
    return key


def provider_settings(config: dict | None = None) -> list[dict]:
    gateway = (config or {}).get("gateway", {})
    if gateway.get("enabled", False):
        return [{
            "id": "clippers-gateway",
            "api_key": gateway.get("client_api_key", "clippers-local"),
            "base_url": gateway.get("base_url", "http://127.0.0.1:8767/v1"),
            "model": gateway.get("model", "clippers-default"),
            "disable_thinking": gateway.get("disable_thinking", True),
        }]
    configured = (config or {}).get("providers", [])
    if configured:
        return [item | {"api_key": _provider_key(item)} for item in configured if item.get("enabled", True)]
    return [
        {"id": "zhipu", "api_key": os.getenv("ZHIPU_API_KEY"),
         "base_url": os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
         "model": os.getenv("ZHIPU_MODEL", "glm-4.7-flash")},
        {"id": "deepseek", "api_key": os.getenv("DEEPSEEK_API_KEY"),
         "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
         "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")},
    ]


def json_completion(messages: list[dict], max_tokens: int = 8192, config: dict | None = None) -> str:
    """Call the configured model route, normally the local Clippers gateway."""
    providers = provider_settings(config)
    errors = []
    for provider in providers:
        name, key = provider["id"], provider.get("api_key")
        base_url, model = provider["base_url"], provider["model"]
        if not key:
            errors.append(f"{name}: API key 未配置")
            continue
        client = OpenAI(api_key=key, base_url=base_url)
        for attempt in range(5):
            try:
                request = dict(
                    model=model, messages=messages, response_format={"type": "json_object"},
                    temperature=float(provider.get("temperature", 0.2)),
                    max_tokens=min(max_tokens, int(provider.get("max_tokens", max_tokens))),
                )
                if provider.get("disable_thinking", True):
                    request["extra_body"] = {"thinking": {"type": "disabled"}}
                response = client.chat.completions.create(**request)
                content = response.choices[0].message.content
                if not content:
                    raise ValueError(f"{name} 返回空内容")
                return content
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                retryable = any(code in str(exc) for code in ("429", "1305", "500", "502", "503", "504"))
                if retryable and attempt < 4:
                    time.sleep(2 ** (attempt + 1))
                    continue
                break
    raise RuntimeError("所有大模型提供方均不可用；" + " | ".join(errors[-4:]))


def test_provider(provider: dict) -> dict:
    key = _provider_key(provider)
    if not key:
        return {"ok": False, "error": "API key 未配置"}
    client = OpenAI(api_key=key, base_url=provider["base_url"])
    response = client.chat.completions.create(model=provider["model"],
        messages=[{"role": "user", "content": "Return JSON: {\"ok\":true}"}],
        response_format={"type": "json_object"}, max_tokens=64)
    return {"ok": bool(response.choices), "model": provider["model"]}
