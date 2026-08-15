from __future__ import annotations

import importlib

from argon2 import PasswordHasher
from fastapi.testclient import TestClient


def test_console_auth_and_config(tmp_path, monkeypatch):
    config = tmp_path / "config"; config.mkdir()
    source = __import__("pathlib").Path(__file__).parents[1] / "config"
    for name in ("runtime.yaml","coporations.yaml","media.yaml","papers.yaml"):
        text = (source / name).read_text(encoding="utf-8")
        if name == "runtime.yaml":
            text = text.replace("data/clippers.db", str(tmp_path / "data/clippers.db")).replace("data/reports", str(tmp_path / "data/reports"))
        (config / name).write_text(text, encoding="utf-8")
    monkeypatch.setenv("CLIPPERS_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLIPPERS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPERLAB_ADMIN_PASSWORD_HASH", PasswordHasher().hash("secret"))
    import clippers_daily.web as web
    web = importlib.reload(web)
    with TestClient(web.app, base_url="https://testserver") as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/paperlab/", follow_redirects=False).status_code == 303
        assert client.get("/api/sync/status").status_code == 404
        login = client.post("/api/login", json={"username":"admin","password":"secret"})
        assert login.status_code == 200
        csrf = login.json()["csrf"]
        assert client.get("/api/config/runtime").status_code == 200
        assert client.put("/api/runtime/schedule", json={"target_items": 8}).status_code == 403
        saved = client.put("/api/runtime/schedule", headers={"X-CSRF-Token":csrf}, json={"target_items": 8})
        assert saved.status_code == 200
        revision = saved.json()["revision"]
        assert client.get("/api/config/runtime").json()["value"]["target_items"] == 8
        assert client.post(f"/api/config/runtime/rollback?revision={revision}", headers={"X-CSRF-Token":csrf}).status_code == 200
        assert client.get("/api/config/runtime").json()["value"]["target_items"] == 10
        assert client.put("/api/config/runtime", headers={"X-CSRF-Token":csrf}, json={"raw_yaml":"version: 1\ntarget_items: 0\n","confirm":True}).status_code == 400
        secret_yaml = "version: 1\ntarget_items: 7\nllm:\n  providers:\n    - id: bad\n      base_url: https://example.invalid\n      model: x\n      api_key: exposed\n"
        assert client.put("/api/config/runtime", headers={"X-CSRF-Token":csrf},
                          json={"raw_yaml":secret_yaml,"confirm":True}).status_code == 400
        with web.STORE.db:
            web.STORE.db.execute("INSERT INTO deliveries VALUES (?,?,?)", ("2026-08-14", "now", "m1"))
        duplicate = client.post("/api/jobs/daily-send", headers={"X-CSRF-Token":csrf},
                                json={"date":"2026-08-14","confirm":True})
        assert duplicate.status_code == 409
        assert client.get("/api/digests").status_code == 200
