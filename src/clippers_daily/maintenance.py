from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .collectors import collect_source
from .config import Settings
from .llm import json_completion
from .source_adapter import adapter_path, run_source_adapter, validate_adapter_code
from .storage import Store


GOOD = {"success", "not_modified", "available", "delegated_to_papers"}
AUTH_ERRORS = ("permission denied", "authentication failed", "could not read username", "publickey",
               "repository not found", "access denied")


def _source_failures(store: Store, settings: Settings) -> list[dict]:
    rows = store.db.execute("""SELECT source_id,status,fetched,parsed,selected,error,run_id
      FROM source_runs ORDER BY rowid DESC""").fetchall()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["source_id"], []).append(row)
    threshold = int(settings.runtime.get("maintenance", {}).get("consecutive_failures", 2))
    empty_threshold = int(settings.runtime.get("maintenance", {}).get("empty_run_threshold", 3))
    result = []
    for source_id, history in grouped.items():
        recent = history[:max(threshold, empty_threshold)]
        broken = len(recent) >= threshold and all(r["status"] not in GOOD for r in recent[:threshold])
        empty = len(recent) >= empty_threshold and all(r["status"] == "success" and not r["parsed"] for r in recent[:empty_threshold])
        if broken or empty:
            last_repair = store.db.execute("SELECT status,next_retry_at FROM maintenance_runs WHERE target_type='source' AND target_id=? ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()
            if last_repair and last_repair["next_retry_at"] and last_repair["next_retry_at"] > datetime.now(timezone.utc).isoformat():
                continue
            result.append({"source_id": source_id, "trigger": "consecutive_failure" if broken else "repeated_empty",
                           "history": [dict(r) for r in recent]})
    return result


def _research(source: dict, history: list[dict]) -> dict:
    urls = []
    for channel in source.get("channels", []):
        if channel.get("endpoint"): urls.append(channel["endpoint"])
    if source.get("endpoint"): urls.append(source["endpoint"])
    pages = []
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Clippers-Maintainer/1.0"}) as client:
        for url in urls[:5]:
            try:
                response = client.get(url)
                pages.append({"url": str(response.url), "status": response.status_code,
                              "content_type": response.headers.get("content-type"), "sample": response.text[:12000]})
            except Exception as exc:
                pages.append({"url": url, "error": str(exc)[:500]})
        try:
            query = f"{source.get('name', source['id'])} official RSS sitemap API"
            response = client.get("https://html.duckduckgo.com/html/", params={"q": query})
            links = re.findall(r'nofollow" class="result__a" href="([^"]+)', response.text)
            pages.append({"search": query, "results": links[:10]})
        except Exception as exc:
            pages.append({"search_error": str(exc)[:500]})
    return {"source": source, "failures": history, "evidence": pages}


def _extract_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S)
    return json.loads(content)


def _generate_adapter(source: dict, evidence: dict, settings: Settings) -> tuple[str, str]:
    prompt = """你是 Clippers 信息源维护工程师。根据真实错误、页面样本和搜索结果，为且仅为当前信息源编写独立 Python 采集适配器。
严格返回 JSON {"diagnosis":"中文诊断","code":"完整Python源码"}。源码必须定义 collect(source, runtime)，返回
{"records":[Record字段字典],"reports":[RunReport字段字典]}。只允许导入 re/json/datetime/urllib.parse/httpx/feedparser/bs4、
clippers_daily.models、clippers_daily.collectors；禁止文件、进程、环境变量和动态执行。使用官方 RSS/API/sitemap 优先，保留发布时间、标题、摘要、canonical URL；网络请求必须有 timeout。
不得伪造内容；来源确实没有近期更新时允许返回 success 和空 records。
诊断材料：""" + json.dumps(evidence, ensure_ascii=False)
    response = json_completion([{"role": "user", "content": prompt}], max_tokens=12000,
                               config=settings.runtime.get("llm", {}))
    value = _extract_json(response)
    code = value.get("code", "")
    validate_adapter_code(code)
    return str(value.get("diagnosis", "")), code


def _quality(records, reports) -> dict:
    complete = sum(bool(r.title.strip()) and r.url.startswith("http") and r.parse_status == "complete" for r in records)
    unique = len({r.url for r in records})
    success = bool(reports) and all(r.status in GOOD for r in reports)
    return {"success": success, "records": len(records), "complete": complete, "unique_urls": unique,
            "statuses": [r.status for r in reports], "errors": [r.error for r in reports if r.error]}


def _accept_quality(before: dict, after: dict, trigger: str) -> bool:
    if not after["success"]:
        return False
    if after["records"]:
        return after["complete"] == after["records"] and after["unique_urls"] == after["records"]
    # An empty result cannot demonstrate that a repeatedly-empty source was repaired.
    return False


def _git_publish(source_id: str, candidate: Path, settings: Settings) -> str:
    config = settings.runtime.get("maintenance", {})
    worktree = Path(config.get("worktree", "/srv/clippers/maintenance/repo"))
    remote = config.get("repository", settings.runtime.get("git", {}).get("remote"))
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if not (worktree / ".git").is_dir():
        subprocess.run(["git", "clone", remote, str(worktree)], check=True, timeout=120)
    subprocess.run(["git", "-C", str(worktree), "pull", "--ff-only", "origin", "main"], check=True, timeout=120)
    target = worktree / "source_adapters" / candidate.name
    target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(candidate, target)
    subprocess.run(["python3", "-m", "py_compile", str(target)], check=True, timeout=30)
    subprocess.run(["git", "-C", str(worktree), "add", str(target)], check=True)
    if subprocess.run(["git", "-C", str(worktree), "diff", "--cached", "--quiet"]).returncode:
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Clippers Maintainer",
                        "-c", "user.email=maintainer@paperlab.cloud", "commit", "-m",
                        f"Auto-repair source adapter: {source_id}"], check=True, timeout=60)
        subprocess.run(["git", "-C", str(worktree), "push", "origin", "main"], check=True, timeout=120)
    return subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()


def repair_source(problem: dict, settings: Settings, store: Store) -> dict:
    source_id = problem["source_id"]; source = settings.source(source_id)
    started = datetime.now(timezone.utc); cursor = store.db.execute(
        "INSERT INTO maintenance_runs(started_at,target_type,target_id,trigger,status) VALUES (?,?,?,?,?)",
        (started.isoformat(), "source", source_id, problem["trigger"], "running")); repair_id = cursor.lastrowid
    store.db.commit()
    try:
        before_records, before_reports = collect_source(settings, source_id)
        before = _quality(before_records, before_reports)
        evidence = _research(source, problem["history"])
        diagnosis, code = _generate_adapter(source, evidence, settings)
        with tempfile.TemporaryDirectory(prefix="clippers-repair-") as directory:
            candidate = Path(directory) / (adapter_path(source_id).name)
            candidate.write_text(code, encoding="utf-8")
            records, reports = run_source_adapter(source, settings.runtime, candidate)
            after = _quality(records, reports)
            if not _accept_quality(before, after, problem["trigger"]):
                raise ValueError(f"质量验收未通过: {after}")
            sha = _git_publish(source_id, candidate, settings)
            deployed = adapter_path(source_id); deployed.parent.mkdir(parents=True, exist_ok=True)
            temporary = deployed.with_suffix(".py.new"); shutil.copy2(candidate, temporary); os.replace(temporary, deployed)
        status, error = "deployed", None
        next_retry = None
    except Exception as exc:
        diagnosis = locals().get("diagnosis", "")
        before, after, sha, deployed = locals().get("before", {}), locals().get("after", {}), None, None
        status, error = "failed", str(exc)[:2000]
        attempts = store.db.execute("SELECT count(*) FROM maintenance_runs WHERE target_type='source' AND target_id=?", (source_id,)).fetchone()[0]
        next_retry = (datetime.now(timezone.utc) + timedelta(hours=min(24, 2 ** min(attempts, 4)))).isoformat()
    with store.db:
        store.db.execute("""UPDATE maintenance_runs SET finished_at=?,status=?,diagnosis=?,action=?,quality_before=?,quality_after=?,git_sha=?,deployed_path=?,error=?,next_retry_at=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), status, diagnosis, "generate_test_commit_hot_deploy",
             json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), sha,
             str(deployed) if deployed else None, error, next_retry, repair_id))
        store.log(f"maintenance:{repair_id}", "info" if status == "deployed" else "error", "maintenance",
                  f"信息源 {source_id} 自动修复{status}", {"git_sha": sha, "error": error, "quality": after})
    return {"id": repair_id, "source_id": source_id, "status": status, "git_sha": sha, "error": error}


def repair_failed_digest(settings: Settings, store: Store) -> dict | None:
    today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    if store.was_delivered(today): return None
    row = store.db.execute("SELECT * FROM daily_runs WHERE digest_date=? AND status='failed' ORDER BY started_at DESC LIMIT 1", (today,)).fetchone()
    if not row: return None
    attempts = store.db.execute("SELECT count(*) FROM maintenance_runs WHERE target_type='digest' AND target_id=?", (today,)).fetchone()[0]
    maximum = int(settings.runtime.get("maintenance", {}).get("max_attempts_per_target", 3))
    if attempts >= maximum: return {"status": "exhausted", "date": today}
    started = datetime.now(timezone.utc); cursor = store.db.execute(
        "INSERT INTO maintenance_runs(started_at,target_type,target_id,trigger,status) VALUES (?,?,?,?,?)",
        (started.isoformat(), "digest", today, "daily_run_failed", "running")); repair_id = cursor.lastrowid; store.db.commit()
    try:
        prompt = "分析日报失败原因并给下一次编辑生成一段简短、可执行的修复指令。不要修改事实和硬配额。错误：" + str(row["error"])
        diagnosis = json_completion([{"role":"user","content":prompt}], max_tokens=1000, config=settings.runtime.get("llm", {}))
        hint = settings.database.parent / "digest_repair_prompt.txt"; hint.write_text(diagnosis, encoding="utf-8")
        from .app import run_daily
        run_daily(Settings(), date.fromisoformat(today), send=True, force_send=False, mode="auto-repair")
        status, error = "deployed", None
    except Exception as exc:
        status, error = "failed", str(exc)[:2000]
    with store.db:
        store.db.execute("UPDATE maintenance_runs SET finished_at=?,status=?,diagnosis=?,action=?,error=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), status, locals().get("diagnosis", ""), "regenerate_and_send", error, repair_id))
        store.log(f"maintenance:{repair_id}", "info" if not error else "error", "maintenance", "日报自动修复完成", {"date":today,"status":status,"error":error})
    return {"id": repair_id, "date": today, "status": status, "error": error}


def _notes_git_state(config: dict) -> dict:
    repo = Path(config.get("repo_path", ""))
    branch = config.get("branch", "main")
    if not (repo / ".git").is_dir():
        return {"healthy": False, "repo": str(repo), "branch": branch, "error": "笔记仓库不存在或未初始化"}
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if env.get("CLIPPERS_NOTES_GIT_SSH_COMMAND"):
            env["GIT_SSH_COMMAND"] = env["CLIPPERS_NOTES_GIT_SSH_COMMAND"]
        return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True,
                              timeout=60, env=env)
    fetch = git("fetch", "origin", branch)
    if fetch.returncode:
        error = (fetch.stderr or fetch.stdout).strip()[:2000]
        return {"healthy": False, "repo": str(repo), "branch": branch, "error": error,
                "needs_personal_info": any(token in error.lower() for token in AUTH_ERRORS)}
    status = git("status", "--porcelain")
    counts = git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}")
    if status.returncode or counts.returncode:
        error = (status.stderr or counts.stderr or status.stdout or counts.stdout).strip()[:2000]
        return {"healthy": False, "repo": str(repo), "branch": branch, "error": error}
    ahead, behind = (int(value) for value in counts.stdout.split())
    dirty = [line[3:] for line in status.stdout.splitlines() if len(line) > 3]
    return {"healthy": not dirty and not ahead and not behind, "repo": str(repo), "branch": branch,
            "ahead": ahead, "behind": behind, "dirty": dirty, "error": ""}


def repair_notes_git(settings: Settings, store: Store) -> dict | None:
    config = settings.runtime.get("notes", {})
    if not config.get("enabled", False):
        return None
    state = _notes_git_state(config)
    if state.get("healthy"):
        return {"status": "healthy", "state": state}
    started = datetime.now(timezone.utc)
    cursor = store.db.execute(
        "INSERT INTO maintenance_runs(started_at,target_type,target_id,trigger,status,quality_before) VALUES (?,?,?,?,?,?)",
        (started.isoformat(), "git", "daily-notes", "git_sync_unhealthy", "running",
         json.dumps(state, ensure_ascii=False)))
    repair_id = cursor.lastrowid; store.db.commit()
    diagnosis, action, error = "", "diagnose", state.get("error", "")
    status = "blocked_personal_info" if state.get("needs_personal_info") else "manual_review"
    try:
        if not state.get("needs_personal_info"):
            prompt = """诊断日报笔记 Git 同步状态。严格返回 JSON {"diagnosis":"中文", "action":"pull_ff|push|commit_inbox|manual_review"}。
只允许：干净且仅落后时 pull_ff；仅领先时 push；修改文件全部位于 _inbox/日报/ 时 commit_inbox；分叉或其他文件修改必须 manual_review。状态：""" + json.dumps(state, ensure_ascii=False)
            advice = _extract_json(json_completion([{"role": "user", "content": prompt}], max_tokens=800,
                                                   config=settings.runtime.get("llm", {})))
            diagnosis = str(advice.get("diagnosis", "")); suggested = advice.get("action")
            repo, branch = Path(state["repo"]), state["branch"]
            allowed = "manual_review"
            if state.get("behind", 0) and not state.get("ahead", 0) and not state.get("dirty"):
                allowed = "pull_ff"
            elif state.get("ahead", 0) and not state.get("behind", 0) and not state.get("dirty"):
                allowed = "push"
            elif state.get("dirty") and all(path.startswith("_inbox/日报/") for path in state["dirty"]):
                allowed = "commit_inbox"
            action = suggested if suggested == allowed else allowed
            if action == "pull_ff":
                subprocess.run(["git", "-C", str(repo), "pull", "--ff-only", "origin", branch], check=True, timeout=120)
            elif action == "push":
                subprocess.run(["git", "-C", str(repo), "push", "origin", branch], check=True, timeout=120)
            elif action == "commit_inbox":
                subprocess.run(["git", "-C", str(repo), "add", "--", "_inbox/日报"], check=True)
                subprocess.run(["git", "-C", str(repo), "commit", "-m", "repair: publish pending daily inbox"], check=True)
                subprocess.run(["git", "-C", str(repo), "push", "origin", branch], check=True, timeout=120)
            if action != "manual_review":
                after = _notes_git_state(config)
                if not after.get("healthy"):
                    raise RuntimeError(f"Git 修复后仍不健康: {after}")
                status, error = "deployed", ""
        after = locals().get("after", state)
    except Exception as exc:
        error = str(exc)[:2000]
        status = "blocked_personal_info" if any(token in error.lower() for token in AUTH_ERRORS) else "failed"
        after = _notes_git_state(config)
    with store.db:
        store.db.execute("""UPDATE maintenance_runs SET finished_at=?,status=?,diagnosis=?,action=?,quality_after=?,error=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), status, diagnosis, action,
             json.dumps(after, ensure_ascii=False), error, repair_id))
        store.log(f"maintenance:{repair_id}", "info" if status == "deployed" else "warning", "maintenance",
                  f"日报 Git 同步维护：{status}", {"action": action, "error": error})
    return {"id": repair_id, "status": status, "action": action, "error": error}


def maintain(settings: Settings | None = None) -> dict:
    settings = settings or Settings(); store = Store(settings.database)
    if not settings.runtime.get("maintenance", {}).get("enabled", True): return {"enabled": False}
    problems = _source_failures(store, settings)
    limit = int(settings.runtime.get("maintenance", {}).get("max_repairs_per_run", 2))
    source_results = [repair_source(problem, settings, store) for problem in problems[:limit]]
    digest_result = repair_failed_digest(settings, store)
    git_result = repair_notes_git(settings, store)
    return {"enabled": True, "problems": len(problems), "source_repairs": source_results,
            "digest_repair": digest_result, "git_repair": git_result}
