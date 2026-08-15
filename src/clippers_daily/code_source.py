from __future__ import annotations

import hashlib
import base64
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .llm import json_completion
from .models import Record, RunReport


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_json(path: str, value: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".new")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, target)


def github_oauth_config(code: dict) -> dict:
    return code.get("github", {})


def github_connect_url(code: dict, state: str, verifier: str) -> str:
    github = github_oauth_config(code)
    client_id = _read(github.get("client_id_file"))
    if not client_id:
        raise ValueError("GitHub App Client ID 未配置")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urlencode({"client_id": client_id, "redirect_uri": github["callback_url"],
                       "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    return github.get("authorize_url", "https://github.com/login/oauth/authorize") + "?" + query


def exchange_github_code(code_config: dict, code_value: str, verifier: str) -> dict:
    github = github_oauth_config(code_config)
    client_id = _read(github.get("client_id_file"))
    client_secret = _read(github.get("client_secret_file"))
    if not client_id or not client_secret:
        raise ValueError("GitHub App Client ID 或 Client Secret 未配置")
    response = httpx.post(github.get("token_url", "https://github.com/login/oauth/access_token"),
        headers={"Accept": "application/json"}, timeout=30, data={
            "client_id": client_id, "client_secret": client_secret, "code": code_value,
            "redirect_uri": github["callback_url"], "code_verifier": verifier,
        })
    response.raise_for_status()
    token = response.json()
    if not token.get("access_token"):
        raise ValueError(token.get("error_description") or "GitHub 未返回 Access Token")
    token["obtained_at"] = _now().isoformat()
    _write_json(github["token_file"], token)
    return token


def _token(code: dict) -> dict:
    path = github_oauth_config(code).get("token_file")
    try:
        token = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, json.JSONDecodeError):
        return {}
    obtained = token.get("obtained_at")
    expires_in = int(token.get("expires_in", 0) or 0)
    if token.get("refresh_token") and obtained and expires_in:
        expires = datetime.fromisoformat(obtained) + timedelta(seconds=expires_in)
        if expires <= _now() + timedelta(minutes=5):
            github = github_oauth_config(code)
            try:
                response = httpx.post(github.get("token_url", "https://github.com/login/oauth/access_token"),
                    headers={"Accept": "application/json"}, timeout=30, data={
                        "client_id": _read(github.get("client_id_file")),
                        "client_secret": _read(github.get("client_secret_file")),
                        "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                    })
                response.raise_for_status()
                refreshed = response.json()
                if refreshed.get("access_token"):
                    refreshed["obtained_at"] = _now().isoformat()
                    _write_json(path, refreshed); token = refreshed
            except httpx.HTTPError:
                pass
    return token


class GitHubClient:
    def __init__(self, code: dict):
        token = _token(code).get("access_token")
        if not token:
            raise ValueError("GitHub 账户尚未关联")
        self.base = github_oauth_config(code).get("api_url", "https://api.github.com").rstrip("/")
        self.client = httpx.Client(timeout=45, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "clippers-daily",
        })

    def get(self, path: str, **kwargs) -> httpx.Response:
        response = self.client.get(self.base + path, **kwargs)
        if response.status_code == 429 or (response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
            raise RuntimeError("GitHub API 限流，重置时间=" + response.headers.get("x-ratelimit-reset", "unknown"))
        response.raise_for_status()
        return response

    def pages(self, path: str, params: dict | None = None, limit: int = 20) -> list[dict]:
        rows = []
        for page in range(1, limit + 1):
            response = self.get(path, params={**(params or {}), "per_page": 100, "page": page})
            batch = response.json()
            rows.extend(batch)
            if len(batch) < 100:
                break
        return rows


def code_status(store, code: dict) -> dict:
    account = store.db.execute("SELECT * FROM code_accounts WHERE provider='github'").fetchone()
    counts = store.db.execute("SELECT count(*),sum(muted) FROM code_repositories WHERE provider='github'").fetchone()
    github = github_oauth_config(code)
    return {
        "provider": "github", "connected": bool(_token(code).get("access_token")),
        "app_configured": bool(_read(github.get("client_id_file")) and _read(github.get("client_secret_file"))),
        "account": dict(account) if account else None, "repositories": counts[0] or 0,
        "muted_repositories": counts[1] or 0, "gitlab": {"available": False, "configured": False},
    }


def _repo_head(client: GitHubClient, repo: dict) -> str:
    branch = repo.get("default_branch") or "main"
    return client.get(f"/repos/{repo['full_name']}/commits/{branch}").json()["sha"]


def _analysis(change: dict, llm_config: dict, max_bytes: int) -> dict:
    files = []
    size = 0
    truncated = False
    for item in change.get("files", []):
        patch = item.get("patch") or ""
        row = {"filename": item.get("filename"), "status": item.get("status"),
               "additions": item.get("additions"), "deletions": item.get("deletions"), "patch": patch}
        encoded = json.dumps(row, ensure_ascii=False).encode()
        if size + len(encoded) > max_bytes:
            truncated = True
            break
        files.append(row); size += len(encoded)
    payload = {"repository": change["repository"], "commits": change.get("commits", [])[:100],
               "files": files, "release": change.get("release"), "truncated": truncated}
    prompt = """分析公开代码仓库自上次快照以来的改动。只依据输入证据，严格返回 JSON：
{"summary":"中文详细总结","functional_changes":[],"architecture_api":[],"performance_security":[],
 "upgrade_risks":[],"importance_score":0,"confidence":0.0,"important":false,"routine_only":false}
重要更新包括正式发布、破坏性 API、关键安全修复、核心架构或显著性能变化；纯文档、格式、测试、CI、例行依赖更新不重要。
输入：""" + json.dumps(payload, ensure_ascii=False)
    content = json_completion([{"role": "user", "content": prompt}], max_tokens=4096, config=llm_config)
    result = json.loads(content)
    result["truncated"] = truncated
    result["importance_score"] = max(0, min(100, int(result.get("importance_score", 0))))
    return result


def _record(repo: dict, base: str, head: str, comparison: dict, analysis: dict, threshold: int) -> Record:
    full_name = repo["full_name"]
    url = f"https://github.com/{full_name}/compare/{base}...{head}"
    important = bool(analysis.get("important")) or analysis["importance_score"] >= threshold or bool(comparison.get("release"))
    analysis["important"] = important and not analysis.get("routine_only", False)
    return Record(id=f"github:{full_name}:{base[:12]}:{head[:12]}", category="code",
        title=f"{full_name} 代码更新", url=url, source_name=full_name, source_id="github-starred",
        channel_id=full_name, published_at=_now(), discovered_at=_now(), collected_at=_now(),
        summary=analysis.get("summary", "")[:4000], priority="P0" if analysis["important"] else "P2",
        source_priority=0 if analysis["important"] else 2, source_urls=[url, repo.get("html_url", url)],
        metadata={"provider": "github", "repository": full_name, "base_sha": base, "head_sha": head,
                  "important_code": analysis["important"], "importance_score": analysis["importance_score"],
                  "analysis": analysis})


def sync_github(code: dict, store, llm_config: dict, repository: str | None = None) -> tuple[list[Record], RunReport]:
    started = _now(); records = []; fetched = parsed = filtered = 0
    try:
        client = GitHubClient(code)
        user = client.get("/user").json()
        starred = client.pages("/user/starred", {"sort": "updated", "direction": "desc"})
        starred = [r for r in starred if not r.get("private")]
        if repository:
            starred = [r for r in starred if r.get("full_name", "").lower() == repository.lower()]
        following_error = ""
        try:
            following = [] if repository else [x for x in client.pages("/user/following") if x.get("type") == "Organization"]
        except httpx.HTTPStatusError as exc:
            following = []
            following_error = f"Following 频道不可用：HTTP {exc.response.status_code}，请检查 Followers 只读权限"
        fetched = len(starred) + len(following)
        existing_count = store.db.execute("SELECT count(*) FROM code_repositories WHERE provider='github'").fetchone()[0]
        threshold = int(code.get("importance_threshold", 75)); max_bytes = int(code.get("max_diff_bytes", 200000))
        for repo in starred:
            full_name = repo["full_name"]
            row = store.db.execute("SELECT last_sha,muted,last_checked_at FROM code_repositories WHERE provider='github' AND full_name=?", (full_name,)).fetchone()
            head = _repo_head(client, repo)
            with store.lock, store.db:
                store.db.execute("""INSERT INTO code_repositories(provider,full_name,html_url,default_branch,last_sha,starred_at,muted,private,last_checked_at,metadata)
                  VALUES ('github',?,?,?,?,?,?,0,?,?) ON CONFLICT(provider,full_name) DO UPDATE SET
                  html_url=excluded.html_url,default_branch=excluded.default_branch,last_checked_at=excluded.last_checked_at,metadata=excluded.metadata""",
                  (full_name, repo["html_url"], repo.get("default_branch"), head, repo.get("starred_at"),
                   int(row["muted"]) if row else 0, started.isoformat(), json.dumps({"description": repo.get("description"), "stars": repo.get("stargazers_count")}, ensure_ascii=False)))
            if not row or not row["last_sha"] or row["last_sha"] == head or row["muted"]:
                filtered += 1; continue
            base = row["last_sha"]
            try:
                compared = client.get(f"/repos/{full_name}/compare/{base}...{head}").json()
            except httpx.HTTPStatusError:
                compared = {"commits": client.get(f"/repos/{full_name}/commits", params={"per_page": 30}).json(),
                            "files": [], "fallback": "recent_commits"}
            releases = client.get(f"/repos/{full_name}/releases", params={"per_page": 5}).json()
            release = next((r for r in releases if r.get("target_commitish") in {repo.get("default_branch"), head}
                            and (not row["last_checked_at"] or (r.get("published_at") or "") > row["last_checked_at"])), None)
            comparison = {"repository": full_name, "commits": [{"sha": c.get("sha"), "message": c.get("commit", {}).get("message")}
                for c in compared.get("commits", [])], "files": compared.get("files", []), "release": release}
            try:
                analysis = _analysis(comparison, llm_config, max_bytes)
                record = _record(repo, base, head, comparison, analysis, threshold)
                status = "analyzed"; records.append(record); parsed += 1
            except Exception as exc:
                analysis = {"error": str(exc)[:1000]}; status = "analysis_failed"; record = None
            with store.lock, store.db:
                store.db.execute("""INSERT OR REPLACE INTO code_changes
                  (provider,full_name,base_sha,head_sha,detected_at,published_at,status,important,importance_score,analysis,record_id)
                  VALUES ('github',?,?,?,?,?,?,?,?,?,?)""", (full_name, base, head, started.isoformat(), started.isoformat(), status,
                    int(bool(record and record.metadata["important_code"])), analysis.get("importance_score"),
                    json.dumps(analysis, ensure_ascii=False), record.id if record else None))
                if status == "analyzed":
                    store.db.execute("UPDATE code_repositories SET last_sha=? WHERE provider='github' AND full_name=?", (head, full_name))
        if not repository:
            active_names = {repo["full_name"] for repo in starred}
            with store.lock, store.db:
                for tracked in store.db.execute("SELECT full_name FROM code_repositories WHERE provider='github'").fetchall():
                    if tracked[0] not in active_names:
                        store.db.execute("DELETE FROM code_repositories WHERE provider='github' AND full_name=?", (tracked[0],))
        # One global organization rollup, based on repositories pushed since the previous account sync.
        account = store.db.execute("SELECT last_sync_at FROM code_accounts WHERE provider='github'").fetchone()
        cutoff = account[0] if account and account[0] else started.isoformat()
        org_updates = []
        for org in following:
            for repo in client.get(f"/orgs/{org['login']}/repos", params={"sort": "pushed", "per_page": 20}).json():
                if (repo.get("pushed_at") or "") > cutoff:
                    org_updates.append({"organization": org["login"], "repository": repo["full_name"],
                                        "pushed_at": repo.get("pushed_at"), "description": repo.get("description")})
        if org_updates:
            summary = "；".join(f"{x['repository']}：{x.get('description') or '有新提交'}" for x in org_updates[:30])
            records.append(Record(id="github:following-orgs:" + started.date().isoformat(), category="code",
                title="关注 GitHub 组织更新概览", url="https://github.com", source_name="GitHub 关注组织",
                source_id="github-following", channel_id="organizations", published_at=started, discovered_at=started,
                collected_at=started, summary=summary[:4000], priority="P1", source_priority=1,
                metadata={"provider": "github", "organization_rollup": True, "updates": org_updates[:30]}))
        token = _token(code)
        expires_at = None
        if token.get("obtained_at") and token.get("expires_in"):
            expires_at = (datetime.fromisoformat(token["obtained_at"]) + timedelta(seconds=int(token["expires_in"]))).isoformat()
        with store.lock, store.db:
            store.db.execute("""INSERT INTO code_accounts(provider,external_id,login,status,scopes,token_expires_at,last_sync_at,updated_at)
              VALUES ('github',?,?, 'connected',?,?,?,?) ON CONFLICT(provider) DO UPDATE SET
              external_id=excluded.external_id,login=excluded.login,status='connected',scopes=excluded.scopes,
              token_expires_at=excluded.token_expires_at,last_sync_at=excluded.last_sync_at,updated_at=excluded.updated_at""",
              (str(user.get("id")), user.get("login"), json.dumps(token.get("scope", "").split(",")),
               expires_at, started.isoformat(), started.isoformat()))
        return records, RunReport(source_id="github-account", channel_id="starred-following", status="success",
            fetched=fetched, parsed=parsed, selected=len(records), filtered=filtered, error=following_error or None,
            started_at=started, finished_at=_now())
    except Exception as exc:
        return records, RunReport(source_id="github-account", channel_id="starred-following", status="fetch_error",
            fetched=fetched, parsed=parsed, selected=len(records), filtered=filtered, error=str(exc)[:1000],
            started_at=started, finished_at=_now())


def disconnect_github(code: dict, store) -> None:
    token_file = github_oauth_config(code).get("token_file")
    if token_file:
        try: Path(token_file).unlink()
        except FileNotFoundError: pass
    with store.lock, store.db:
        store.db.execute("DELETE FROM code_accounts WHERE provider='github'")
        store.db.execute("UPDATE code_repositories SET last_sha=NULL WHERE provider='github'")
