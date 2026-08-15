from datetime import datetime, timezone

import pytest

from clippers_daily.maintenance import _accept_quality, _notes_git_state
from clippers_daily.source_adapter import run_source_adapter, validate_adapter_code


def test_adapter_sandbox_rejects_filesystem_import():
    with pytest.raises(ValueError, match="禁止导入"):
        validate_adapter_code("import os\ndef collect(source, runtime): return {}")


def test_independent_adapter_executes_in_subprocess(tmp_path):
    adapter = tmp_path / "demo.py"
    adapter.write_text('''from clippers_daily.models import Record, RunReport
from datetime import datetime, timezone
def collect(source, runtime):
    now = datetime.now(timezone.utc)
    return [Record(id="demo:1", source_id=source["id"], source_name=source["name"], channel_id="custom", category="media", title="Demo", url="https://example.com/1", published_at=now, discovered_at=now, collected_at=now, summary="ok", language="zh-CN", priority="P1")], [RunReport(source_id=source["id"], channel_id="custom", status="success", fetched=1, parsed=1)]
''', encoding="utf-8")
    records, reports = run_source_adapter({"id": "demo", "name": "Demo"}, {}, adapter)
    assert records[0].title == "Demo"
    assert reports[0].parsed == 1


def test_repair_quality_requires_real_records():
    assert _accept_quality({}, {"success": True, "records": 1, "complete": 1, "unique_urls": 1}, "consecutive_failure")
    assert not _accept_quality({}, {"success": True, "records": 0, "complete": 0, "unique_urls": 0}, "repeated_empty")


def test_notes_git_state_detects_clean_synced_repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "--bare", str(tmp_path / "remote")], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(tmp_path / "remote"), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "main"], check=True, capture_output=True)
    (repo / "README.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], check=True, capture_output=True)
    assert _notes_git_state({"repo_path": str(repo), "branch": "main"})["healthy"]
