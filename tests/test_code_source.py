from datetime import datetime, timezone

from clippers_daily.app import _candidate_pool
from clippers_daily.code_source import sync_github
from clippers_daily.models import Record
from clippers_daily.storage import Store


class FakeResponse:
    def __init__(self, value): self.value = value
    def json(self): return self.value


class FakeGitHub:
    head = "a" * 40
    def __init__(self, code): pass
    def pages(self, path, params=None, limit=20):
        if path == "/user/starred":
            return [{"full_name":"acme/project","html_url":"https://github.com/acme/project",
                     "default_branch":"main","private":False,"description":"demo","stargazers_count":7}]
        return [{"login":"acme","type":"Organization"}]
    def get(self, path, **kwargs):
        if path == "/user": return FakeResponse({"id":1,"login":"james"})
        if "/commits/main" in path: return FakeResponse({"sha":self.head})
        if "/compare/" in path: return FakeResponse({"commits":[{"sha":self.head,"commit":{"message":"feat: faster engine"}}],"files":[]})
        if path.endswith("/releases"): return FakeResponse([])
        if path.startswith("/orgs/"): return FakeResponse([])
        raise AssertionError(path)


def test_first_sync_only_builds_baseline_then_analyzes_change(tmp_path, monkeypatch):
    import clippers_daily.code_source as source
    store = Store(tmp_path / "db.sqlite")
    monkeypatch.setattr(source, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(source, "_analysis", lambda *args: {"summary":"重要功能变化","important":True,
        "routine_only":False,"importance_score":90,"confidence":.9})
    records, report = sync_github({"importance_threshold":75}, store, {})
    assert records == [] and report.status == "success"
    assert store.db.execute("SELECT last_sha FROM code_repositories").fetchone()[0] == "a" * 40
    FakeGitHub.head = "b" * 40
    records, report = sync_github({"importance_threshold":75}, store, {})
    assert len(records) == 1 and records[0].category == "code"
    assert records[0].metadata["important_code"] is True
    assert store.db.execute("SELECT count(*) FROM code_changes").fetchone()[0] == 1


def test_important_code_expands_daily_target_to_fifteen(tmp_path):
    class Settings:
        fallback_days=7; lookback_hours=36; minimum_deepseek_items=1; minimum_zh_media_items=1
        minimum_paperlab_items=1; target_items=10; maximum_items=15
        papers={"sources":[{"selection":{"topic_filter":{"keywords":[]}}}]}
    now = datetime.now(timezone.utc); records=[]
    for index in range(8):
        records.append(Record(id=f"code-{index}",category="code",title=f"Repo {index}",url=f"https://github.com/a/r{index}",
            source_name=f"a/r{index}",source_id="github-starred",channel_id=f"a/r{index}",published_at=now,
            collected_at=now,summary="change",priority="P0",metadata={"important_code":True,"importance_score":90-index}))
    for identifier, category, source in (("deep","company","deepseek"),("zh","media","jiqizhixin"),("paper","paper","paperlab")):
        records.append(Record(id=identifier,category=category,title=identifier,url=f"https://example.com/{identifier}",
            source_name=source,source_id=source,channel_id="x",published_at=now,collected_at=now,
            language="zh-CN" if identifier=="zh" else "en",summary="x",priority="P0"))
    _, policy = _candidate_pool(Settings(), Store(tmp_path / "pool.sqlite"), records, now)
    assert policy["target_items"] == 15
    assert policy["minimum_important_code_items"] == 8
