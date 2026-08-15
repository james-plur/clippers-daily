from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from .app import run_daily
from .auth import AuthManager, Session, secret_status
from .collectors import collect_source
from .code_source import (code_status, disconnect_github, exchange_github_code,
                          github_connect_url, sync_github)
from .config import Settings
from .config_service import ConfigManager, SECTIONS
from .llm import test_provider
from .mailer import test_connection
from .maintenance import maintain
from .storage import Store


ROOT = Path(__file__).resolve().parent
TEMPLATES = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
SETTINGS = Settings()
STORE = Store(SETTINGS.database)
CONFIG = ConfigManager(SETTINGS.config_dir, STORE)
AUTH = AuthManager(int(SETTINGS.runtime.get("web", {}).get("session_ttl_seconds", 86400)), SETTINGS.database)
JOB_LOCK = threading.Lock()
OAUTH_STATES: dict[str, tuple[str, float]] = {}
COOKIE = "clippers_session"


def project_root() -> Path:
    cwd = Path.cwd()
    return cwd if (cwd / "pyproject.toml").is_file() else Path(__file__).resolve().parents[2]


class LoginBody(BaseModel):
    username: str
    password: str


class LikeBody(BaseModel):
    liked: bool


class ConfigBody(BaseModel):
    raw_yaml: str
    confirm: bool = False


class SourceBody(BaseModel):
    enabled: bool | None = None
    priority: str | None = None


class JobBody(BaseModel):
    date: str | None = None
    source_id: str | None = None
    confirm: bool = False
    force: bool = False


class SecretBody(BaseModel):
    value: str = Field(min_length=1)


class GitHubAppBody(BaseModel):
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)


class RepositoryBody(BaseModel):
    muted: bool


class RepositoryTestBody(BaseModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def write_secret(path: Path, value: str) -> None:
    """Write a service secret without leaking filesystem exceptions to clients."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.strip() + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        raise HTTPException(503, "密钥存储暂时不可写，请检查服务目录权限") from exc


def reject_secret_fields(value: object) -> None:
    """Keep credentials out of YAML and API echoes."""
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in {"api_key", "password", "token", "private_key"}:
                raise HTTPException(400, f"{key} 必须通过密钥接口保存")
            reject_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            reject_secret_fields(child)


def current_session(clippers_session: str | None = Cookie(default=None)) -> Session:
    session = AUTH.get(clippers_session)
    if not session:
        raise HTTPException(401, "需要登录")
    return session


def optional_session(clippers_session: str | None = Cookie(default=None)) -> Session | None:
    return AUTH.get(clippers_session)


def csrf_session(session: Session = Depends(current_session), x_csrf_token: str = Header(default="")) -> Session:
    if not x_csrf_token or not hmac_compare(x_csrf_token, session.csrf):
        raise HTTPException(403, "CSRF 校验失败")
    return session


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left, right)


def paperlab_gateway_token() -> str:
    configured = os.getenv("PAPERLAB_GATEWAY_TOKEN", "").strip()
    if configured:
        return configured
    proxy = CONFIG.read("runtime").get("paperlab_proxy", {})
    token_file = Path(proxy.get("gateway_token_file", "/srv/clippers/secrets/paperlab_gateway_token"))
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def create_app() -> FastAPI:
    app = FastAPI(title="Clippers Console", version="0.2.0", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, "database": SETTINGS.database.is_file(), "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return HTMLResponse(TEMPLATES.get_template("login.html").render())

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict:
        session = AUTH.login(body.username, body.password, request.client.host if request.client else "unknown")
        if not session:
            raise HTTPException(401, "账号或密码错误")
        forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
        response.set_cookie(COOKIE, session.token, httponly=True, secure=forwarded == "https",
                            samesite="strict", max_age=AUTH.ttl, path="/")
        return {"ok": True, "csrf": session.csrf}

    @app.post("/api/logout")
    def logout(response: Response, token: str | None = Cookie(default=None, alias=COOKIE)) -> dict:
        AUTH.logout(token)
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index(session: Session | None = Depends(optional_session)) -> Response:
        if not session:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(TEMPLATES.get_template("index.html").render(csrf=session.csrf))

    @app.get("/api/status")
    def status(session: Session = Depends(current_session)) -> dict:
        latest = STORE.list_jobs(1)
        reports = [dict(row) for row in STORE.db.execute(
            """SELECT s.source_id,s.status,s.fetched,s.parsed,s.eligible,s.selected,s.error
               FROM source_runs s JOIN (
                 SELECT source_id,max(rowid) AS latest_row FROM source_runs GROUP BY source_id
               ) latest ON latest.latest_row=s.rowid ORDER BY s.source_id""")]
        return {"latest_job": latest[0] if latest else None, "source_health": reports,
                "digests": STORE.list_digests(7), "secrets": secret_status(CONFIG.read("runtime")),
                "paperlab": paperlab_status()}

    @app.get("/api/digests")
    def digests(limit: int = 30, session: Session = Depends(current_session)) -> list[dict]:
        return STORE.list_digests(min(365, max(1, limit)))

    @app.get("/api/digests/{digest_date}")
    def digest(digest_date: str, session: Session = Depends(current_session)) -> dict:
        value = STORE.get_digest(digest_date)
        if not value:
            raise HTTPException(404, "日报不存在")
        return value

    @app.put("/api/digests/{digest_date}/items/{item_id}/like")
    def like_item(digest_date: str, item_id: str, body: LikeBody, session: Session = Depends(csrf_session)) -> dict:
        try:
            STORE.set_like(digest_date, item_id, body.liked)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        audit("like.item", f"{digest_date}/{item_id}", {"liked": body.liked})
        return {"ok": True, "liked": body.liked}

    @app.get("/api/sources")
    def sources(session: Session = Depends(current_session)) -> list[dict]:
        values = []
        for section_name, key in (("corporations", "corporations"), ("media", "sources"), ("papers", "sources")):
            section = CONFIG.read(section_name)
            for source in section.get(key, []):
                last = STORE.db.execute("""SELECT run_id,status,fetched,eligible,selected,error FROM source_runs
                  WHERE source_id=? ORDER BY run_id DESC LIMIT 1""", (source.get("id"),)).fetchone()
                values.append({"id": source.get("id"), "name": source.get("name"), "enabled": source.get("enabled", False),
                               "priority": source.get("priority", "P2"), "language": source.get("language", "auto"),
                               "channels": len(source.get("channels", [])) or 1, "last_run": dict(last) if last else None})
        return values

    @app.get("/api/code/status")
    def get_code_status(session: Session = Depends(current_session)) -> dict:
        return code_status(STORE, CONFIG.read("code"))

    @app.put("/api/secrets/code/github-app")
    def put_github_app(body: GitHubAppBody, session: Session = Depends(csrf_session)) -> dict:
        secret_dir = Path(os.getenv("CLIPPERS_SECRET_DIR", "/srv/clippers/secrets"))
        write_secret(secret_dir / "github_app_client_id", body.client_id)
        write_secret(secret_dir / "github_app_client_secret", body.client_secret)
        audit("code.github_app.configure", "github", {})
        return {"ok": True, "configured": True}

    @app.get("/api/code/github/connect")
    def github_connect(session: Session = Depends(current_session)) -> RedirectResponse:
        import secrets
        import time
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        OAUTH_STATES[state] = (verifier, time.time() + 600)
        try:
            return RedirectResponse(github_connect_url(CONFIG.read("code"), state, verifier), status_code=303)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/code/github/callback")
    def github_callback(code: str, state: str) -> RedirectResponse:
        import time
        pending = OAUTH_STATES.pop(state, None)
        if not pending or pending[1] < time.time():
            raise HTTPException(400, "GitHub OAuth state 无效或已过期")
        try:
            exchange_github_code(CONFIG.read("code"), code, pending[0])
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(502, f"GitHub 授权失败：{exc}") from exc
        audit("code.github.connect", "github", {})
        return RedirectResponse("/#code", status_code=303)

    @app.post("/api/code/github/disconnect")
    def github_disconnect(session: Session = Depends(csrf_session)) -> dict:
        disconnect_github(CONFIG.read("code"), STORE)
        audit("code.github.disconnect", "github", {})
        return {"ok": True}

    @app.get("/api/code/repositories")
    def code_repositories(session: Session = Depends(current_session)) -> list[dict]:
        return [dict(row) for row in STORE.db.execute(
            "SELECT provider,full_name,html_url,default_branch,last_sha,starred_at,muted,last_checked_at,metadata "
            "FROM code_repositories ORDER BY muted,full_name")]

    @app.put("/api/code/repositories/{provider}/{owner}/{repo}")
    def update_code_repository(provider: str, owner: str, repo: str, body: RepositoryBody,
                               session: Session = Depends(csrf_session)) -> dict:
        with STORE.lock, STORE.db:
            cursor = STORE.db.execute("UPDATE code_repositories SET muted=? WHERE provider=? AND full_name=?",
                                      (int(body.muted), provider, f"{owner}/{repo}"))
        if not cursor.rowcount:
            raise HTTPException(404, "仓库不存在")
        audit("code.repository.mute" if body.muted else "code.repository.unmute", f"{provider}/{owner}/{repo}", {})
        return {"ok": True, "muted": body.muted}

    @app.post("/api/jobs/code-sync")
    def code_sync(session: Session = Depends(csrf_session)) -> dict:
        if not JOB_LOCK.acquire(blocking=False):
            raise HTTPException(409, "已有任务运行中")
        run_id = "code:" + datetime.now(timezone.utc).isoformat()
        def runner():
            try:
                STORE.log(run_id, "info", "code", "GitHub 账户同步开始")
                records, report = sync_github(CONFIG.read("code"), STORE, CONFIG.read("runtime").get("llm", {}))
                STORE.save_records(records); STORE.save_reports(run_id, [report])
                STORE.log(run_id, "info" if report.status == "success" else "error", "code",
                          "GitHub 账户同步完成", report.model_dump(mode="json"))
            finally:
                JOB_LOCK.release()
        threading.Thread(target=runner, daemon=True).start()
        return {"ok": True, "run_id": run_id, "status": "started"}

    @app.post("/api/jobs/code-repository-test")
    def code_repository_test(body: RepositoryTestBody, session: Session = Depends(csrf_session)) -> dict:
        records, report = sync_github(CONFIG.read("code"), STORE, CONFIG.read("runtime").get("llm", {}), body.repository)
        return {"records": [r.model_dump(mode="json") for r in records], "report": report.model_dump(mode="json")}

    @app.put("/api/sources/{source_id}")
    def update_source(source_id: str, body: SourceBody, session: Session = Depends(csrf_session)) -> dict:
        for section_name, key in (("corporations", "corporations"), ("media", "sources"), ("papers", "sources")):
            value = CONFIG.read(section_name)
            source = next((item for item in value.get(key, []) if item.get("id") == source_id), None)
            if source is None:
                continue
            if body.enabled is not None:
                source["enabled"] = body.enabled
            if body.priority is not None:
                if body.priority not in {"P0", "P1", "P2", "P3"}:
                    raise HTTPException(400, "优先级必须为 P0-P3")
                source["priority"] = body.priority
            revision = CONFIG.write(section_name, value)
            return {"ok": True, "revision": revision}
        raise HTTPException(404, "信息源不存在")

    @app.get("/api/config/{section}")
    def get_config(section: str, session: Session = Depends(current_session)) -> dict:
        value = CONFIG.read(section)
        return {"section": section, "value": value,
                "raw_yaml": yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
                "revisions": CONFIG.revisions(section)}

    @app.put("/api/config/{section}")
    def put_config(section: str, body: ConfigBody, session: Session = Depends(csrf_session)) -> dict:
        if not body.confirm:
            raise HTTPException(409, "必须确认配置变更")
        try:
            value = yaml.safe_load(body.raw_yaml) or {}
            reject_secret_fields(value)
            revision = CONFIG.write(section, value)
        except (ValueError, yaml.YAMLError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "revision": revision}

    @app.put("/api/runtime/{area}")
    def put_runtime_area(area: str, body: dict, session: Session = Depends(csrf_session)) -> dict:
        if area not in {"email", "llm", "git", "paperlab", "schedule"}:
            raise HTTPException(404, "运行配置区段不存在")
        reject_secret_fields(body)
        runtime = CONFIG.read("runtime")
        if area == "schedule":
            for key in ("target_items", "lookback_hours", "fallback_days", "minimum_deepseek_items",
                        "minimum_zh_media_items", "minimum_media_items", "minimum_paper_items",
                        "minimum_paperlab_items", "maximum_items"):
                if key in body:
                    runtime[key] = body[key]
        else:
            runtime[area] = body
        try:
            revision = CONFIG.write("runtime", runtime)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "revision": revision}

    @app.post("/api/config/{section}/rollback")
    def rollback_config(section: str, revision: int, session: Session = Depends(csrf_session)) -> dict:
        try:
            return {"ok": True, "revision": CONFIG.rollback(section, revision)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.put("/api/secrets/llm/{provider_id}")
    def put_llm_secret(provider_id: str, body: SecretBody, session: Session = Depends(csrf_session)) -> dict:
        runtime = CONFIG.read("runtime")
        provider = next((item for item in runtime.get("llm", {}).get("providers", []) if item.get("id") == provider_id), None)
        if not provider:
            raise HTTPException(404, "模型提供方不存在")
        secret_dir = Path(os.getenv("CLIPPERS_SECRET_DIR", "/srv/clippers/secrets"))
        path = secret_dir / f"{provider_id}_api_key"
        write_secret(path, body.value)
        provider["api_key_file"] = str(path)
        CONFIG.write("runtime", runtime)
        return {"ok": True, "configured": True}

    @app.put("/api/secrets/smtp/{sender_id}")
    def put_smtp_secret(sender_id: str, body: SecretBody, session: Session = Depends(csrf_session)) -> dict:
        runtime = CONFIG.read("runtime")
        sender = next((item for item in runtime.get("email", {}).get("senders", []) if item.get("id") == sender_id), None)
        if not sender:
            raise HTTPException(404, "发件配置不存在")
        secret_dir = Path(os.getenv("CLIPPERS_SECRET_DIR", "/srv/clippers/secrets"))
        path = secret_dir / f"smtp_{sender_id}_password"
        write_secret(path, body.value)
        sender["password_file"] = str(path)
        CONFIG.write("runtime", runtime)
        return {"ok": True, "configured": True}

    @app.post("/api/mail/test")
    def mail_test(session: Session = Depends(csrf_session)) -> dict:
        return test_connection(CONFIG.read("runtime").get("email", {}))

    @app.post("/api/models/{provider_id}/test")
    def model_test(provider_id: str, session: Session = Depends(csrf_session)) -> dict:
        runtime = CONFIG.read("runtime")
        provider = next((item for item in runtime.get("llm", {}).get("providers", []) if item.get("id") == provider_id), None)
        if not provider:
            raise HTTPException(404, "模型提供方不存在")
        return test_provider(provider)

    @app.get("/api/git/status")
    def git_status(session: Session = Depends(current_session)) -> dict:
        repo = project_root()
        configured = CONFIG.read("runtime").get("git", {})
        revision_file = repo / "REVISION"
        if not (repo / ".git").exists() and revision_file.is_file():
            return {"branch": configured.get("branch", "main"),
                    "commit": revision_file.read_text(encoding="utf-8").strip()[:12],
                    "remote": configured.get("remote", ""), "status": "immutable release",
                    "configured": configured}
        def run(*args: str) -> str:
            result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=10)
            return (result.stdout or result.stderr).strip()
        return {"branch": run("branch", "--show-current"), "commit": run("rev-parse", "--short", "HEAD"),
                "remote": run("remote", "get-url", "origin"), "status": run("status", "--short"),
                "configured": configured}

    @app.post("/api/git/test")
    def git_test(session: Session = Depends(csrf_session)) -> dict:
        repo = project_root()
        config = CONFIG.read("runtime").get("git", {})
        remote = str(config.get("remote") or "origin")
        env = os.environ.copy()
        key_file = config.get("ssh_key_file")
        if key_file:
            env["GIT_SSH_COMMAND"] = f"ssh -i {key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        result = subprocess.run(["git", "-C", str(repo), "ls-remote", "--heads", remote],
                                text=True, capture_output=True, timeout=20, env=env)
        return {"ok": result.returncode == 0, "error": "" if result.returncode == 0 else result.stderr[-500:]}

    @app.post("/api/jobs/source-test")
    def source_test(body: JobBody, session: Session = Depends(csrf_session)) -> dict:
        if not body.source_id:
            raise HTTPException(400, "缺少 source_id")
        settings = Settings()
        try:
            records, reports = collect_source(settings, body.source_id)
        except ValueError as exc:
            raise HTTPException(404, "信息源不存在") from exc
        return {"records": [record.model_dump(mode="json") for record in records if record.source_id == body.source_id][:50],
                "reports": [report.model_dump(mode="json") for report in reports if report.source_id == body.source_id]}

    @app.post("/api/jobs/daily-preview")
    def daily_preview(body: JobBody, session: Session = Depends(csrf_session)) -> dict:
        return enqueue_daily(body.date or date.today().isoformat(), False, False)

    @app.post("/api/jobs/daily-send")
    def daily_send(body: JobBody, session: Session = Depends(csrf_session)) -> dict:
        if not body.confirm:
            raise HTTPException(409, "正式发送需要二次确认")
        return enqueue_daily(body.date or date.today().isoformat(), True, body.force)

    @app.get("/api/jobs")
    def jobs(limit: int = 50, session: Session = Depends(current_session)) -> list[dict]:
        return STORE.list_jobs(min(200, max(1, limit)))

    @app.get("/api/logs")
    def logs(limit: int = 200, component: str | None = None, session: Session = Depends(current_session)) -> list[dict]:
        return STORE.list_logs(min(1000, max(1, limit)), component)

    @app.get("/api/maintenance")
    def maintenance_runs(limit: int = 100, session: Session = Depends(current_session)) -> list[dict]:
        rows = STORE.db.execute("SELECT * FROM maintenance_runs ORDER BY id DESC LIMIT ?", (min(500, max(1, limit)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("quality_before", "quality_after"):
                try:
                    item[key] = json.loads(item[key] or "{}")
                except json.JSONDecodeError:
                    item[key] = {}
            result.append(item)
        return result

    @app.post("/api/jobs/maintenance")
    def maintenance_job(session: Session = Depends(csrf_session)) -> dict:
        if not JOB_LOCK.acquire(blocking=False):
            raise HTTPException(409, "已有后台任务正在运行")
        def worker() -> None:
            try:
                maintain(Settings())
            finally:
                JOB_LOCK.release()
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "status": "started"}

    @app.get("/api/paperlab/status")
    def get_paperlab_status(session: Session = Depends(current_session)) -> dict:
        return paperlab_status()

    @app.post("/api/paperlab/sync")
    def paperlab_sync(body: JobBody, session: Session = Depends(csrf_session)) -> dict:
        if not body.confirm:
            raise HTTPException(409, "PaperLab 同步需要二次确认")
        command = CONFIG.read("runtime").get("paperlab", {}).get("sync_command")
        if not command:
            raise HTTPException(400, "未配置 PaperLab 同步命令")
        if not isinstance(command, list) or not all(isinstance(part, str) and part for part in command):
            raise HTTPException(400, "PaperLab 同步命令必须是参数数组")
        subprocess.Popen(command, start_new_session=True)
        audit("paperlab.sync", "paperlab", {})
        return {"ok": True, "status": "started"}

    @app.api_route("/api/sync/{path:path}", methods=["GET", "HEAD", "POST"])
    async def paperlab_device_sync(path: str, request: Request) -> Response:
        # Keep the existing device protocol public while never exposing PaperLab's
        # administrator-only sync status, conflict or device-registration routes.
        if not (path in {"handshake", "pull", "push"} or re.fullmatch(r"objects/[0-9a-f]{64}", path)):
            raise HTTPException(404, "sync route not found")
        if request.method == "POST" and path != "push":
            raise HTTPException(405, "method not allowed")
        runtime = CONFIG.read("runtime")
        upstream = runtime.get("paperlab_proxy", {}).get("upstream", "http://127.0.0.1:8765")
        url = f"{upstream.rstrip('/')}/api/sync/{path}"
        if request.url.query:
            url += "?" + request.url.query
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in {"host", "content-length", "cookie"}}
        async with httpx.AsyncClient(timeout=3600, follow_redirects=False) as client:
            result = await client.request(request.method, url, headers=headers, content=await request.body())
        response_headers = {key: value for key, value in result.headers.items()
                            if key.lower() not in {"content-length", "content-encoding", "transfer-encoding",
                                                   "connection", "set-cookie"}}
        return Response(result.content, status_code=result.status_code, headers=response_headers)

    @app.get("/paperlab")
    def paperlab_redirect(session: Session | None = Depends(optional_session)) -> RedirectResponse:
        if not session:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/paperlab/", status_code=307)

    @app.api_route("/paperlab/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def paperlab_proxy(path: str, request: Request,
                             session: Session | None = Depends(optional_session)) -> Response:
        if not session:
            if request.method == "GET" and not path:
                return RedirectResponse("/login", status_code=303)
            raise HTTPException(401, "需要登录")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac_compare(request.headers.get("x-csrf-token", ""), session.csrf):
                raise HTTPException(403, "CSRF 校验失败")
        upstream = CONFIG.read("runtime").get("paperlab_proxy", {}).get("upstream", "http://127.0.0.1:8765")
        url = f"{upstream.rstrip('/')}/{path}"
        if request.url.query:
            url += "?" + request.url.query
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in {"host", "content-length", "cookie", "authorization",
                                          "x-clippers-gateway-token"}}
        headers["X-Forwarded-Prefix"] = "/paperlab"
        gateway_token = paperlab_gateway_token()
        if not gateway_token:
            raise HTTPException(503, "PaperLab 网关凭据未配置")
        headers["X-Clippers-Gateway-Token"] = gateway_token
        async with httpx.AsyncClient(timeout=3600, follow_redirects=False) as client:
            result = await client.request(request.method, url, headers=headers, content=await request.body())
        response_headers = {key: value for key, value in result.headers.items()
                            if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection", "set-cookie"}}
        if "location" in response_headers and response_headers["location"].startswith("/"):
            response_headers["location"] = "/paperlab" + response_headers["location"]
        content = result.content
        content_type = result.headers.get("content-type", "application/octet-stream")
        if path == "api/auth/session" and result.status_code == 200:
            try:
                payload = result.json()
                payload.update({"authenticated": True, "trusted_local": True, "csrf_token": session.csrf})
                content = json.dumps(payload, ensure_ascii=False).encode()
                content_type = "application/json; charset=utf-8"
            except ValueError:
                pass
        elif content_type.startswith("text/html"):
            text = content.decode(result.encoding or "utf-8")
            for prefix in ("api", "pdf", "reports"):
                text = text.replace(f'"/{prefix}/', f'"/paperlab/{prefix}/')
                text = text.replace(f"'/{prefix}/", f"'/paperlab/{prefix}/")
            content = text.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        response_headers["content-type"] = content_type
        return Response(content, status_code=result.status_code, headers=response_headers)

    return app


def audit(action: str, target: str, detail: dict) -> None:
    with STORE.lock, STORE.db:
        STORE.db.execute("INSERT INTO audit_events(happened_at,actor,action,target,detail) VALUES (?,?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), "admin", action, target, json.dumps(detail, ensure_ascii=False)))


def enqueue_daily(digest_date: str, send: bool, force: bool) -> dict:
    try:
        date.fromisoformat(digest_date)
    except ValueError as exc:
        raise HTTPException(400, "日期格式错误") from exc
    if send and STORE.was_delivered(digest_date) and not force:
        raise HTTPException(409, f"{digest_date} 已发送；显式重发必须使用 force")
    if JOB_LOCK.locked():
        raise HTTPException(409, "已有日报任务正在运行")
    request_id = str(uuid.uuid4())
    def runner() -> None:
        with JOB_LOCK:
            try:
                run_daily(Settings(), date.fromisoformat(digest_date), send=send, force_send=force,
                          mode="send" if send else "preview")
            except Exception as exc:
                with STORE.db:
                    STORE.log(request_id, "error", "web", "后台任务失败", {"error": str(exc)[:1000]})
    threading.Thread(target=runner, daemon=True).start()
    audit("daily.send" if send else "daily.preview", digest_date, {"request_id": request_id, "force": force})
    return {"ok": True, "status": "queued", "request_id": request_id}


def paperlab_status() -> dict:
    path = Path(CONFIG.read("runtime").get("paperlab", {}).get("database", "/nonexistent"))
    result = {"database": str(path), "available": path.is_file(), "papers": 0, "sync_runs": []}
    if not path.is_file():
        return result
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        result["papers"] = db.execute("SELECT count(*) FROM papers WHERE deleted_at IS NULL").fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_runs'").fetchone():
            columns = [row[1] for row in db.execute("PRAGMA table_info(sync_runs)")]
            rows = db.execute("SELECT * FROM sync_runs ORDER BY rowid DESC LIMIT 10").fetchall()
            result["sync_runs"] = [dict(zip(columns, row)) for row in rows]
        db.close()
    except Exception as exc:
        result["error"] = str(exc)[:500]
    return result


app = create_app()


def main() -> None:
    web = SETTINGS.runtime.get("web", {})
    uvicorn.run("clippers_daily.web:app", host=web.get("host", "127.0.0.1"), port=int(web.get("port", 8766)))
