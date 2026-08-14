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
from .config import Settings
from .config_service import ConfigManager, SECTIONS
from .llm import test_provider
from .mailer import test_connection
from .storage import Store


ROOT = Path(__file__).resolve().parent
TEMPLATES = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
SETTINGS = Settings()
STORE = Store(SETTINGS.database)
CONFIG = ConfigManager(SETTINGS.config_dir, STORE)
AUTH = AuthManager(int(SETTINGS.runtime.get("web", {}).get("session_ttl_seconds", 86400)))
JOB_LOCK = threading.Lock()
COOKIE = "clippers_session"


def project_root() -> Path:
    cwd = Path.cwd()
    return cwd if (cwd / "pyproject.toml").is_file() else Path(__file__).resolve().parents[2]


class LoginBody(BaseModel):
    username: str
    password: str


class RatingBody(BaseModel):
    rating: int = Field(ge=1, le=5)
    review: str = ""


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


def csrf_session(session: Session = Depends(current_session), x_csrf_token: str = Header(default="")) -> Session:
    if not x_csrf_token or not hmac_compare(x_csrf_token, session.csrf):
        raise HTTPException(403, "CSRF 校验失败")
    return session


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left, right)


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
    def index(session: Session = Depends(current_session)) -> HTMLResponse:
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

    @app.put("/api/digests/{digest_date}/rating")
    def rate_digest(digest_date: str, body: RatingBody, session: Session = Depends(csrf_session)) -> dict:
        STORE.rate("digest", digest_date, "digest", body.rating, body.review)
        audit("rating.digest", digest_date, {"rating": body.rating})
        return {"ok": True}

    @app.put("/api/digests/{digest_date}/items/{item_id}/rating")
    def rate_item(digest_date: str, item_id: str, body: RatingBody, session: Session = Depends(csrf_session)) -> dict:
        STORE.rate("item", digest_date, item_id, body.rating, body.review)
        audit("rating.item", f"{digest_date}/{item_id}", {"rating": body.rating})
        return {"ok": True}

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
                        "minimum_zh_media_items", "minimum_media_items", "minimum_paper_items"):
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
        secret_dir.mkdir(parents=True, exist_ok=True)
        path = secret_dir / f"{provider_id}_api_key"
        path.write_text(body.value.strip() + "\n", encoding="utf-8")
        path.chmod(0o600)
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
        secret_dir.mkdir(parents=True, exist_ok=True)
        path = secret_dir / f"smtp_{sender_id}_password"
        path.write_text(body.value.strip() + "\n", encoding="utf-8")
        path.chmod(0o600)
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
    def paperlab_redirect(session: Session = Depends(current_session)) -> RedirectResponse:
        return RedirectResponse("/paperlab/", status_code=307)

    @app.api_route("/paperlab/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def paperlab_proxy(path: str, request: Request, session: Session = Depends(current_session)) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac_compare(request.headers.get("x-csrf-token", ""), session.csrf):
                raise HTTPException(403, "CSRF 校验失败")
        upstream = CONFIG.read("runtime").get("paperlab_proxy", {}).get("upstream", "http://127.0.0.1:8765")
        url = f"{upstream.rstrip('/')}/{path}"
        if request.url.query:
            url += "?" + request.url.query
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in {"host", "content-length", "cookie", "authorization"}}
        headers["X-Forwarded-Prefix"] = "/paperlab"
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
