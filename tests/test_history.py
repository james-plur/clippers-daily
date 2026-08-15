from __future__ import annotations

from pathlib import Path

from clippers_daily.history import parse_digest_markdown
from clippers_daily.storage import Store


LEGACY = """# AI 基础设施日报 · 2026-08-06

这是历史概览。

## 今日速览

1. **[媒体] 机器之心历史文章** — 值得回看。

## 详情

### 1. 机器之心历史文章

- 来源：机器之心
- 收录原因：值得回看。

历史正文。

链接：[https://example.com/article](https://example.com/article)
"""


def test_parse_legacy_digest():
    digest = parse_digest_markdown(LEGACY, "2026-08-06")
    assert digest.overview == "这是历史概览。"
    assert digest.items[0].category == "media"
    assert digest.items[0].source == "机器之心"
    assert digest.items[0].links == ["https://example.com/article"]


def test_backfill_history_is_incremental(tmp_path: Path):
    reports = tmp_path / "reports" / "2026-08-06"
    reports.mkdir(parents=True)
    (reports / "daily.md").write_text(LEGACY, encoding="utf-8")
    (reports / "daily.html").write_text("<html>legacy</html>", encoding="utf-8")
    store = Store(tmp_path / "clippers.db")
    with store.db:
        store.db.execute("INSERT INTO deliveries VALUES (?,?,?)", ("2026-08-06", "2026-08-06T08:00:00+08:00", "m1"))
        store.db.execute("INSERT INTO digest_items VALUES (?,?,?,?)",
                         ("2026-08-06", "record-1", "https://example.com/article", "title"))
    first = store.backfill_digest_editions(tmp_path / "reports")
    second = store.backfill_digest_editions(tmp_path / "reports")
    assert first["imported"] == ["2026-08-06"] and not first["errors"]
    assert second["skipped"] == ["2026-08-06"]
    digest = store.get_digest("2026-08-06")
    assert digest["created_at"] == "2026-08-06T08:00:00+08:00"
    assert digest["items"][0]["record_ids"] == ["record-1"]
