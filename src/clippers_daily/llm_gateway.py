from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import load_yaml
from .llm import _provider_key


app = FastAPI(title="Clippers LLM Gateway", docs_url=None, redoc_url=None)


def _runtime() -> dict:
    config_dir = Path(os.getenv("CLIPPERS_CONFIG_DIR", "config"))
    return load_yaml(config_dir / "runtime.yaml")


def _upstreams() -> list[dict]:
    llm = _runtime().get("llm", {})
    provider_id = llm.get("default_provider", "siliconflow")
    enabled = [p for p in llm.get("providers", []) if p.get("enabled", True)]
    providers = sorted(enabled, key=lambda p: p.get("id") != provider_id)
    if not any(p.get("id") == provider_id for p in providers):
        raise HTTPException(503, f"默认模型提供方 {provider_id} 不可用")
    available = []
    for provider in providers:
        key = _provider_key(provider)
        if key:
            available.append(provider | {"api_key": key})
    if available:
        return available
    raise HTTPException(503, "没有可用的模型 API Key，请在 Clippers 控制台设置 SiliconFlow Key")


def _upstream() -> dict:
    return _upstreams()[0]


@app.get("/health")
def health() -> dict:
    try:
        provider = _upstream()
        return {"ok": True, "provider": provider["id"], "model": provider["model"], "key_configured": True}
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail, "key_configured": False}


@app.get("/v1/models")
def models() -> dict:
    provider = _upstream()
    return {"object": "list", "data": [
        {"id": "clippers-default", "object": "model", "owned_by": "clippers"},
        {"id": provider["model"], "object": "model", "owned_by": provider["id"]},
    ]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    providers = _upstreams()
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "请求体必须是 JSON") from exc
    provider = providers[0]
    payload["model"] = provider["model"]
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(float(provider.get("timeout_seconds", 180)))
    if payload.get("stream"):
        async def stream():
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.is_error:
                        yield await response.aread()
                        return
                    async for chunk in response.aiter_bytes():
                        yield chunk
        return StreamingResponse(stream(), media_type="text/event-stream")
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = None
        for provider in providers:
            payload["model"] = provider["model"]
            url = provider["base_url"].rstrip("/") + "/chat/completions"
            headers["Authorization"] = f"Bearer {provider['api_key']}"
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                break
    assert response is not None
    return JSONResponse(response.json(), status_code=response.status_code)


def main() -> None:
    import uvicorn
    runtime = _runtime().get("llm", {}).get("gateway", {})
    uvicorn.run(app, host=runtime.get("host", "127.0.0.1"), port=int(runtime.get("port", 8767)))
