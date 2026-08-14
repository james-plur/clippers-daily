from __future__ import annotations

import gzip
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from clippers_daily.app import _candidate_pool
from clippers_daily.collectors import Collector, date_from_path, normalize_sitemap_url
from clippers_daily.editor import _validate_digest, build_digest
from clippers_daily.models import Digest, DigestItem, Record
from clippers_daily.storage import Store


def record(identifier: str, *, source: str = "example", category: str = "company", language: str = "en") -> Record:
    return Record(id=identifier, category=category, title=f"Title {identifier}", url=f"https://example.com/{identifier}",
                  source_name=source, source_id=source.lower(), channel_id="news", language=language,
                  published_at=datetime.now(timezone.utc), discovered_at=datetime.now(timezone.utc),
                  collected_at=datetime.now(timezone.utc), summary="technical update", priority="P0",
                  source_urls=[f"https://example.com/{identifier}"])


def test_sitemap_url_repair_and_dates():
    broken = "http://www.jiqizhixin.com/https://www.jiqizhixin.com/articles/2026-08-13-4"
    assert normalize_sitemap_url(broken) == "https://www.jiqizhixin.com/articles/2026-08-13-4"
    assert date_from_path(broken, r"/articles/(\d{4}-\d{2}-\d{2})").date().isoformat() == "2026-08-13"
    assert date_from_path("https://api-docs.deepseek.com/news/news260813").date().isoformat() == "2026-08-13"


def test_machine_heart_gzip_sitemap(monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    xml = f"<urlset><url><loc>http://www.jiqizhixin.com/https://www.jiqizhixin.com/articles/{today}-1</loc></url></urlset>"
    class Response:
        def __init__(self, content=b"", text=""):
            self.content, self.text, self.status_code = content, text, 200
        def raise_for_status(self): pass
    collector = Collector()
    monkeypatch.setattr(collector.client, "get", lambda url, **kwargs: Response(gzip.compress(xml.encode())) if url.endswith(".gz") else Response(text='<h1>高质量机器之心文章</h1><meta name="description" content="摘要">'))
    source = {"id":"jiqizhixin","name":"机器之心","language":"zh-CN","priority":"P0"}
    channel = {"id":"website","endpoint":"https://www.jiqizhixin.com/shared/sitemap.xml.gz","url_filter":"/articles/",
               "article_date_pattern":r"/articles/(\d{4}-\d{2}-\d{2})","priority":"P0"}
    records, report = collector.gzip_sitemap(source, channel)
    assert report.status == "success" and len(records) == 1
    assert records[0].title == "高质量机器之心文章"
    assert records[0].language == "zh-CN"


def test_machine_heart_placeholder_is_degraded(monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    xml = f"<urlset><url><loc>https://www.jiqizhixin.com/articles/{today}-1</loc></url></urlset>"
    class Response:
        def __init__(self, content=b"", text=""):
            self.content, self.text, self.status_code = content, text, 200
        def raise_for_status(self): pass
    collector = Collector()
    monkeypatch.setattr(collector.client, "get", lambda url, **kwargs: Response(gzip.compress(xml.encode())) if url.endswith(".gz") else Response(text="<title>机器之心·数据服务</title>"))
    source = {"id":"jiqizhixin","name":"机器之心","language":"zh-CN","priority":"P0"}
    channel = {"id":"website","endpoint":"https://www.jiqizhixin.com/shared/sitemap.xml.gz","url_filter":"/articles/",
               "article_date_pattern":r"/articles/(\d{4}-\d{2}-\d{2})","priority":"P0"}
    records, report = collector.gzip_sitemap(source, channel)
    assert report.status == "degraded" and report.parsed == 0
    assert records[0].parse_status == "partial"
    assert "不会进入日报" in report.error


def test_deepseek_sitemap_uses_slug_date(monkeypatch):
    class Response:
        content = b'<urlset><url><loc>https://api-docs.deepseek.com/news/news260813</loc></url></urlset>'
        text = '<h1>DeepSeek-V4-Pro GA Release</h1><meta name="description" content="release detail">'
        status_code = 200
        def raise_for_status(self): pass
    collector = Collector()
    xml = Response()
    xml.text = xml.content.decode()
    html = Response()
    monkeypatch.setattr(collector.client, "get", lambda url, **kwargs: xml if url == "x" else html)
    records, report = collector.sitemap("company", "deepseek", "DeepSeek", {"id":"api-news","endpoint":"x","url_filter":"/news/","priority":"P0","detail_lookback_days":365})
    assert report.status == "success" and records[0].published_at.date().isoformat() == "2026-08-13"
    assert records[0].title == "DeepSeek-V4-Pro GA Release"


def test_github_org_collects_recent_repo_and_release(monkeypatch):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    class Response:
        def __init__(self, data, status=200):
            self._data, self.status_code, self.headers = data, status, {}
        def json(self): return self._data
        def raise_for_status(self):
            if self.status_code >= 400: raise httpx.HTTPStatusError("error", request=None, response=None)
    repo = {"full_name":"deepseek-ai/demo","html_url":"https://github.com/deepseek-ai/demo",
            "created_at":now,"updated_at":now,"pushed_at":now,"description":"demo","stargazers_count":9,
            "releases_url":"https://api.github.com/repos/deepseek-ai/demo/releases{/id}"}
    release = {"published_at":now,"html_url":"https://github.com/deepseek-ai/demo/releases/tag/v1",
               "name":"v1","body":"release"}
    collector = Collector()
    monkeypatch.setattr(collector.client, "get", lambda url, **kwargs: Response([repo]) if "/orgs/" in url else Response([release]))
    rows, report = collector.github_org("company", "deepseek", "DeepSeek",
        {"id":"github","endpoint":"https://github.com/deepseek-ai","lookback_days":7,"priority":"P0"})
    assert report.status == "success" and {row.metadata["github_kind"] for row in rows} == {"repository", "release"}


def test_huggingface_org_collects_models_and_datasets(monkeypatch):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    class Response:
        status_code = 200
        def __init__(self, data): self._data = data
        def json(self): return self._data
        def raise_for_status(self): pass
    collector = Collector()
    monkeypatch.setattr(collector.client, "get", lambda url, **kwargs: Response([
        {"id":"deepseek-ai/example","lastModified":now,"tags":["transformers"],"downloads":1}]))
    rows, report = collector.huggingface_org("company", "deepseek", "DeepSeek",
        {"id":"huggingface","endpoint":"https://huggingface.co/deepseek-ai","lookback_days":7,"priority":"P0"})
    assert report.status == "success" and {row.metadata["huggingface_kind"] for row in rows} == {"model", "dataset"}


def test_hard_quotas_are_validated():
    deepseek = record("deep", source="DeepSeek")
    deepseek.source_id = "deepseek"
    chinese = record("zh", source="机器之心", category="media", language="zh-CN")
    paper = record("paper", source="PaperLab", category="paper")
    def item(r):
        return DigestItem(record_ids=[r.id], title=r.title, source=r.source_name, reason="重要", detail="详细说明"*50,
                          links=[r.url], category=r.category, keywords=["推理","训练","系统"], tags=["AI基础设施","日报"])
    digest = Digest(date="2026-08-14", overview="概览", items=[item(deepseek), item(chinese), item(paper)])
    assert _validate_digest(digest, [deepseek, chinese, paper], 3, {"require_deepseek":True,"require_zh_media":True})
    with pytest.raises(ValueError, match="DeepSeek"):
        _validate_digest(Digest(date=digest.date, overview="x", items=[item(chinese), item(paper)]), [deepseek, chinese, paper], 2,
                         {"require_deepseek":True,"require_zh_media":True})


def test_editor_maps_unambiguous_candidate_aliases(monkeypatch):
    deepseek = record("deep", source="DeepSeek"); deepseek.source_id = "deepseek"
    deepseek.metadata["paperlab_id"] = "misleading:internal:id"
    payload = {"date":"2026-08-14","overview":"概览","items":[{
        "record_ids":["c001"],"title":"DeepSeek 更新","source":"DeepSeek","reason":"重要",
        "detail":"详细说明"*50,"links":[deepseek.url],"category":"company",
        "keywords":["推理","训练","系统"],"tags":["AI基础设施","企业"]}]}
    monkeypatch.setattr("clippers_daily.editor.json_completion", lambda *args, **kwargs: __import__("json").dumps(payload, ensure_ascii=False))
    digest = build_digest([deepseek], date(2026,8,14), 1,
                          {"minimum_deepseek_items":1,"reserved_record_ids":[deepseek.id]}, {})
    assert digest.items[0].record_ids == [deepseek.id]


def test_candidate_pool_places_deepseek_and_chinese_first(tmp_path):
    class Settings:
        fallback_days=7; lookback_hours=36; minimum_deepseek_items=1; minimum_zh_media_items=1
        papers={"sources":[{"selection":{"topic_filter":{"keywords":[]}}}]}
    store = Store(tmp_path / "db.sqlite")
    deepseek = record("deep", source="DeepSeek"); deepseek.source_id="deepseek"
    chinese = record("zh", source="机器之心", category="media", language="zh-CN"); chinese.source_id="jiqizhixin"
    other = record("other")
    candidates, policy = _candidate_pool(Settings(), store, [other, chinese, deepseek], datetime.now(timezone.utc))
    assert candidates[:2] == [deepseek, chinese]
    assert policy == {"minimum_deepseek_items": 1, "minimum_zh_media_items": 1,
                      "reserved_record_ids": [deepseek.id, chinese.id]}


def test_storage_migration_history_and_feedback(tmp_path):
    db_path = tmp_path / "clippers.db"
    db = sqlite3.connect(db_path)
    db.executescript("CREATE TABLE deliveries(digest_date TEXT PRIMARY KEY,sent_at TEXT,message_id TEXT); CREATE TABLE digest_items(digest_date TEXT,record_id TEXT,canonical_url TEXT,normalized_title TEXT,PRIMARY KEY(digest_date,record_id)); CREATE TABLE records(id TEXT PRIMARY KEY,category TEXT,title TEXT,canonical_url TEXT,published_at TEXT,collected_at TEXT,payload TEXT,first_seen_at TEXT,last_seen_at TEXT); CREATE TABLE run_reports(run_id TEXT,source_id TEXT,channel_id TEXT,status TEXT,payload TEXT);")
    db.execute("INSERT INTO deliveries VALUES ('2026-08-01','now','m1')"); db.commit(); db.close()
    store = Store(db_path)
    deep = record("deep", source="DeepSeek"); deep.source_id="deepseek"
    item = DigestItem(record_ids=[deep.id],title=deep.title,source="DeepSeek",reason="重要",detail="详情",links=[deep.url],category="company",keywords=["推理","训练","系统"],tags=["AI基础设施","企业"])
    digest = Digest(date="2026-08-14",overview="概览",items=[item])
    store.save_digest(digest,"# markdown","<html>","run")
    entry_id = store.get_digest("2026-08-14")["items"][0]["item_id"]
    store.rate("item","2026-08-14",entry_id,5,"很好")
    assert store.db.execute("SELECT weight FROM preference_weights WHERE dimension='source' AND value='DeepSeek'").fetchone()[0] == .5
    store.decay_preference_weights(datetime.now(timezone.utc) + timedelta(days=1))
    assert store.db.execute("SELECT weight FROM preference_weights WHERE dimension='source' AND value='DeepSeek'").fetchone()[0] == pytest.approx(.5 * .995)
    assert store.was_delivered("2026-08-01")
