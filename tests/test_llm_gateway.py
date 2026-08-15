from fastapi.testclient import TestClient
import respx
from httpx import Response

import clippers_daily.llm_gateway as gateway
from clippers_daily.llm import provider_settings, test_provider as check_provider


def test_clippers_uses_local_gateway():
    providers = provider_settings({"gateway": {"enabled": True}})
    assert providers[0]["base_url"] == "http://127.0.0.1:8767/v1"
    assert providers[0]["model"] == "clippers-default"


@respx.mock
def test_provider_test_returns_upstream_error_instead_of_http_500(monkeypatch):
    monkeypatch.setattr("clippers_daily.llm._provider_key", lambda provider: "secret")
    respx.post("https://api.example/v1/chat/completions").mock(
        return_value=Response(400, json={"error": {"message": "Model does not exist"}}))
    result = check_provider({"id": "example", "base_url": "https://api.example/v1", "model": "missing"})
    assert result["ok"] is False
    assert "Model does not exist" in result["error"]


@respx.mock
def test_gateway_routes_alias_to_default_model(monkeypatch):
    monkeypatch.setattr(gateway, "_runtime", lambda: {"llm": {
        "default_provider": "siliconflow",
        "providers": [{"id": "siliconflow", "enabled": True,
                       "base_url": "https://api.siliconflow.cn/v1",
                       "model": "Pro/deepseek-ai/DeepSeek-V4"}],
    }})
    monkeypatch.setattr(gateway, "_provider_key", lambda provider: "secret")
    route = respx.post("https://api.siliconflow.cn/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    with TestClient(gateway.app) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "clippers-default", "messages": [{"role": "user", "content": "hello"}],
        })
    assert response.status_code == 200
    assert route.calls.last.request.headers["authorization"] == "Bearer secret"
    assert b"Pro/deepseek-ai/DeepSeek-V4" in route.calls.last.request.content


def test_gateway_reports_missing_key(monkeypatch):
    monkeypatch.setattr(gateway, "_runtime", lambda: {"llm": {
        "default_provider": "siliconflow",
        "providers": [{"id": "siliconflow", "enabled": True,
                       "base_url": "https://api.siliconflow.cn/v1", "model": "model"}],
    }})
    monkeypatch.setattr(gateway, "_provider_key", lambda provider: None)
    with TestClient(gateway.app) as client:
        response = client.get("/health")
    assert response.json()["key_configured"] is False


@respx.mock
def test_gateway_falls_back_after_rate_limit(monkeypatch):
    monkeypatch.setattr(gateway, "_runtime", lambda: {"llm": {
        "default_provider": "siliconflow",
        "providers": [
            {"id": "siliconflow", "base_url": "https://one.test/v1", "model": "v4"},
            {"id": "backup", "base_url": "https://two.test/v1", "model": "backup"},
        ],
    }})
    monkeypatch.setattr(gateway, "_provider_key", lambda provider: "secret")
    respx.post("https://one.test/v1/chat/completions").mock(return_value=Response(429, json={"error": "limited"}))
    fallback = respx.post("https://two.test/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    with TestClient(gateway.app) as client:
        response = client.post("/v1/chat/completions", json={"model": "clippers-default", "messages": []})
    assert response.status_code == 200
    assert b'"backup"' in fallback.calls.last.request.content
