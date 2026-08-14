from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .models import Digest, Record


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env.get("CLIPPERS_NOTES_GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = env["CLIPPERS_NOTES_GIT_SSH_COMMAND"]
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=check,
                          text=True, capture_output=True)


def publish_daily_inbox(markdown: str, digest: Digest, records: list[Record], config: dict) -> Path:
    """Publish raw daily input for local knowledge processing; do not create a knowledge note."""
    repo = Path(config["repo_path"])
    branch = config.get("branch", "main")
    if not (repo / ".git").is_dir():
        raise RuntimeError(f"笔记 Git 仓库尚未初始化: {repo}")
    _git(repo, "pull", "--ff-only", "origin", branch)
    inbox = repo / config.get("inbox_dir", "_inbox/日报")
    inbox.mkdir(parents=True, exist_ok=True)
    markdown_path = inbox / f"{digest.date}.md"
    json_path = inbox / f"{digest.date}.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    selected_ids = {value for item in digest.items for value in item.record_ids}
    selected = [record for record in records if record.id in selected_ids]
    payload = {"version": 1, "digest": digest.model_dump(mode="json"),
               "records": [record.model_dump(mode="json") for record in selected]}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths = [str(markdown_path.relative_to(repo)), str(json_path.relative_to(repo))]
    _git(repo, "add", "--", *paths)
    if _git(repo, "diff", "--cached", "--quiet", check=False).returncode:
        _git(repo, "commit", "-m", f"inbox: daily {digest.date}")
        _git(repo, "push", "origin", branch)
    return json_path
