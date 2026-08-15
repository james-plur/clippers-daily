from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .history import parse_digest_markdown
from .models import Record, RunReport


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL,
  canonical_url TEXT NOT NULL, published_at TEXT, collected_at TEXT NOT NULL,
  payload TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_url_idx ON records(canonical_url);
CREATE TABLE IF NOT EXISTS run_reports (
  run_id TEXT NOT NULL, source_id TEXT NOT NULL, channel_id TEXT NOT NULL,
  status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
  digest_date TEXT PRIMARY KEY, sent_at TEXT NOT NULL, message_id TEXT
);
CREATE TABLE IF NOT EXISTS digest_items (
  digest_date TEXT NOT NULL, record_id TEXT NOT NULL, canonical_url TEXT NOT NULL,
  normalized_title TEXT NOT NULL, PRIMARY KEY (digest_date, record_id)
);
CREATE INDEX IF NOT EXISTS digest_items_url_idx ON digest_items(canonical_url);
CREATE TABLE IF NOT EXISTS daily_runs (
  run_id TEXT PRIMARY KEY, digest_date TEXT NOT NULL, mode TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  config_revision INTEGER, markdown_path TEXT, html_path TEXT, message_id TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS daily_runs_date_idx ON daily_runs(digest_date, started_at DESC);
CREATE TABLE IF NOT EXISTS source_runs (
  run_id TEXT NOT NULL, source_id TEXT NOT NULL, channel_id TEXT NOT NULL,
  status TEXT NOT NULL, fetched INTEGER NOT NULL DEFAULT 0,
  parsed INTEGER NOT NULL DEFAULT 0, eligible INTEGER NOT NULL DEFAULT 0,
  selected INTEGER NOT NULL DEFAULT 0, filtered INTEGER NOT NULL DEFAULT 0,
  error TEXT, payload TEXT NOT NULL,
  PRIMARY KEY(run_id, source_id, channel_id)
);
CREATE TABLE IF NOT EXISTS digest_editions (
  digest_date TEXT PRIMARY KEY, overview TEXT NOT NULL, markdown TEXT NOT NULL,
  html TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT
);
CREATE TABLE IF NOT EXISTS digest_entries (
  digest_date TEXT NOT NULL, item_id TEXT NOT NULL, position INTEGER NOT NULL,
  category TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
  reason TEXT NOT NULL, detail TEXT NOT NULL, links TEXT NOT NULL,
  record_ids TEXT NOT NULL, keywords TEXT NOT NULL, tags TEXT NOT NULL,
  PRIMARY KEY(digest_date, item_id)
);
CREATE TABLE IF NOT EXISTS ratings (
  subject_type TEXT NOT NULL, digest_date TEXT NOT NULL, subject_id TEXT NOT NULL,
  rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), review TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL, PRIMARY KEY(subject_type, digest_date, subject_id)
);
CREATE TABLE IF NOT EXISTS preference_weights (
  dimension TEXT NOT NULL, value TEXT NOT NULL, weight REAL NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY(dimension, value)
);
CREATE TABLE IF NOT EXISTS config_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, section TEXT NOT NULL,
  content TEXT NOT NULL, previous_content TEXT, created_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, happened_at TEXT NOT NULL,
  actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS job_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  happened_at TEXT NOT NULL, level TEXT NOT NULL, component TEXT NOT NULL,
  message TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS job_logs_run_idx ON job_logs(run_id, happened_at);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA optimize")
        self._backfill_digest_items()

    @staticmethod
    def normalize_title(title: str) -> str:
        return "".join(ch.lower() for ch in title if ch.isalnum())

    def _backfill_digest_items(self) -> None:
        delivered_dates = {row[0] for row in self.db.execute("SELECT digest_date FROM deliveries")}
        if not delivered_dates:
            return
        with self.lock, self.db:
            for (payload,) in self.db.execute("SELECT payload FROM records"):
                data = json.loads(payload)
                digest_date = data.get("metadata", {}).get("digest_date")
                if digest_date in delivered_dates:
                    self.db.execute("INSERT OR IGNORE INTO digest_items VALUES (?, ?, ?, ?)",
                        (digest_date, data["id"], data["url"], self.normalize_title(data["title"])))

    def save_records(self, records: list[Record]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self.db:
            for record in records:
                self.db.execute(
                    """INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                    last_seen_at=excluded.last_seen_at, title=excluded.title,
                    canonical_url=excluded.canonical_url""",
                    (record.id, record.category, record.title, record.url,
                     record.published_at.isoformat() if record.published_at else None,
                     record.collected_at.isoformat(), record.model_dump_json(), now, now),
                )

    def save_reports(self, run_id: str, reports: list[RunReport]) -> None:
        with self.lock, self.db:
            self.db.executemany(
                "INSERT INTO run_reports VALUES (?, ?, ?, ?, ?)",
                [(run_id, r.source_id, r.channel_id, r.status, r.model_dump_json()) for r in reports],
            )
            self.db.executemany(
                """INSERT OR REPLACE INTO source_runs
                (run_id,source_id,channel_id,status,fetched,parsed,eligible,selected,filtered,error,payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, r.source_id, r.channel_id, r.status, r.fetched, r.parsed,
                  r.eligible, r.selected_for_digest, r.filtered, r.error, r.model_dump_json()) for r in reports],
            )

    def recent_records(self, since: datetime) -> list[Record]:
        """Reload unselected candidates so they remain eligible across daily runs."""
        with self.lock:
            rows = self.db.execute(
                """SELECT payload FROM records
                   WHERE COALESCE(published_at,collected_at)>=? ORDER BY COALESCE(published_at,collected_at) DESC""",
                (since.isoformat(),),
            ).fetchall()
        records = []
        for row in rows:
            try:
                records.append(Record.model_validate_json(row[0]))
            except Exception:
                continue
        return records

    def delivered_identities(self) -> tuple[set[str], set[str], set[str]]:
        rows = self.db.execute("SELECT record_id, canonical_url, normalized_title FROM digest_items").fetchall()
        return ({r[0] for r in rows}, {r[1] for r in rows}, {r[2] for r in rows})

    def was_delivered(self, digest_date: str) -> bool:
        return self.db.execute("SELECT 1 FROM deliveries WHERE digest_date=?", (digest_date,)).fetchone() is not None

    def mark_delivered(self, digest_date: str, message_id: str, records: list[Record]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO deliveries VALUES (?, ?, ?)",
                (digest_date, datetime.now(timezone.utc).isoformat(), message_id),
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO digest_items VALUES (?, ?, ?, ?)",
                [(digest_date, r.id, r.url, self.normalize_title(r.title)) for r in records],
            )

    def start_run(self, run_id: str, digest_date: str, mode: str, config_revision: int | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO daily_runs(run_id,digest_date,mode,status,started_at,config_revision) VALUES (?,?,?,?,?,?)",
                            (run_id, digest_date, mode, "running", datetime.now(timezone.utc).isoformat(), config_revision))
            self.log(run_id, "info", "daily", f"开始{mode}任务", {"digest_date": digest_date})

    def finish_run(self, run_id: str, status: str, markdown_path: str | None = None,
                   html_path: str | None = None, message_id: str | None = None,
                   error: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("""UPDATE daily_runs SET status=?,finished_at=?,markdown_path=?,html_path=?,message_id=?,error=?
                               WHERE run_id=?""",
                            (status, datetime.now(timezone.utc).isoformat(), markdown_path, html_path, message_id, error, run_id))
            self.log(run_id, "error" if status == "failed" else "info", "daily",
                     "任务失败" if status == "failed" else "任务完成", {"error": error} if error else {})

    def save_digest(self, digest, markdown: str, html: str, run_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO digest_editions VALUES (?,?,?,?,?,?)",
                            (digest.date, digest.overview, markdown, html, datetime.now(timezone.utc).isoformat(), run_id))
            self.db.execute("DELETE FROM digest_entries WHERE digest_date=?", (digest.date,))
            for position, item in enumerate(digest.items, 1):
                item_id = hashlib.sha256(("|".join(item.record_ids) + "|" + item.title).encode()).hexdigest()[:16]
                self.db.execute("INSERT INTO digest_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (digest.date, item_id, position, item.category, item.title, item.source, item.reason,
                     item.detail, json.dumps(item.links, ensure_ascii=False), json.dumps(item.record_ids, ensure_ascii=False),
                     json.dumps(item.keywords, ensure_ascii=False), json.dumps(item.tags, ensure_ascii=False)))

    def backfill_digest_editions(self, output_root: Path) -> dict:
        """Import missing historical report files without touching deliveries."""
        result: dict = {"imported": [], "skipped": [], "errors": {}}
        deliveries = self.db.execute(
            "SELECT digest_date,sent_at FROM deliveries ORDER BY digest_date").fetchall()
        for delivery in deliveries:
            digest_date = delivery["digest_date"]
            if self.db.execute("SELECT 1 FROM digest_editions WHERE digest_date=?", (digest_date,)).fetchone():
                result["skipped"].append(digest_date)
                continue
            markdown_path = output_root / digest_date / "daily.md"
            html_path = output_root / digest_date / "daily.html"
            if not markdown_path.is_file() or not html_path.is_file():
                result["errors"][digest_date] = "日报 Markdown 或 HTML 文件不存在"
                continue
            try:
                markdown = markdown_path.read_text(encoding="utf-8")
                html = html_path.read_text(encoding="utf-8")
                digest = parse_digest_markdown(markdown, digest_date)
                url_ids: dict[str, list[str]] = {}
                for row in self.db.execute(
                        "SELECT record_id,canonical_url FROM digest_items WHERE digest_date=?", (digest_date,)):
                    url_ids.setdefault(row["canonical_url"], []).append(row["record_id"])
                for position, item in enumerate(digest.items, 1):
                    record_ids = [identifier for link in item.links for identifier in url_ids.get(link, [])]
                    if not record_ids:
                        seed = "|".join(item.links) or f"{digest_date}|{position}|{item.title}"
                        record_ids = [f"legacy:{hashlib.sha256(seed.encode()).hexdigest()[:20]}"]
                    item.record_ids = list(dict.fromkeys(record_ids))
                run_id = f"legacy-import:{digest_date}"
                self.save_digest(digest, markdown, html, run_id)
                with self.lock, self.db:
                    self.db.execute("UPDATE digest_editions SET created_at=? WHERE digest_date=?",
                                    (delivery["sent_at"], digest_date))
                result["imported"].append(digest_date)
            except Exception as exc:
                result["errors"][digest_date] = str(exc)[:1000]
        return result

    def log(self, run_id: str, level: str, component: str, message: str, detail: dict | None = None) -> None:
        self.db.execute("INSERT INTO job_logs(run_id,happened_at,level,component,message,detail) VALUES (?,?,?,?,?,?)",
                        (run_id, datetime.now(timezone.utc).isoformat(), level, component, message,
                         json.dumps(detail or {}, ensure_ascii=False)))

    def set_like(self, digest_date: str, item_id: str, liked: bool) -> None:
        """Like or unlike an entry and adjust its recommendation dimensions once."""
        now = datetime.now(timezone.utc).isoformat()
        previous = self.db.execute(
            "SELECT rating FROM ratings WHERE subject_type='item' AND digest_date=? AND subject_id=?",
            (digest_date, item_id),
        ).fetchone()
        with self.lock, self.db:
            entry = self.db.execute(
                "SELECT category,source,keywords,tags FROM digest_entries WHERE digest_date=? AND item_id=?",
                (digest_date, item_id),
            ).fetchone()
            if not entry:
                raise ValueError("日报条目不存在")
            old_delta = ((previous["rating"] - 3) * .25) if previous else 0.0
            new_delta = .5 if liked else 0.0
            delta = new_delta - old_delta
            if liked:
                # Rating 5 is retained as the on-disk representation for compatibility.
                self.db.execute("INSERT OR REPLACE INTO ratings VALUES (?,?,?,?,?,?)",
                                ("item", digest_date, item_id, 5, "", now))
            else:
                self.db.execute("DELETE FROM ratings WHERE subject_type='item' AND digest_date=? AND subject_id=?",
                                (digest_date, item_id))
            dimensions = [("category", entry["category"]), ("source", entry["source"])]
            dimensions += [("keyword", value) for value in json.loads(entry["keywords"] or "[]")]
            dimensions += [("tag", value) for value in json.loads(entry["tags"] or "[]")]
            for dimension, value in dimensions:
                if not value or not delta:
                    continue
                self.db.execute("""INSERT INTO preference_weights VALUES (?,?,?,?)
                  ON CONFLICT(dimension,value) DO UPDATE SET
                  weight=max(-5,min(5,preference_weights.weight+excluded.weight)),updated_at=excluded.updated_at""",
                  (dimension, value, delta, now))

    def preference_score(self, record: Record) -> float:
        dimensions = [("category", record.category), ("source", record.source_name)]
        dimensions += [("keyword", value) for value in record.metadata.get("keywords", []) if value]
        dimensions += [("tag", value) for value in record.metadata.get("tags", []) if value]
        score = 0.0
        for dimension, value in dimensions:
            row = self.db.execute("SELECT weight FROM preference_weights WHERE dimension=? AND value=?", (dimension, value)).fetchone()
            score += float(row[0]) if row else 0.0
        return max(-5.0, min(5.0, score))

    def decay_preference_weights(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self.lock, self.db:
            rows = self.db.execute("SELECT dimension,value,weight,updated_at FROM preference_weights").fetchall()
            for row in rows:
                try:
                    updated = datetime.fromisoformat(row["updated_at"])
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    days = max(0, (now.date() - updated.astimezone(timezone.utc).date()).days)
                except (TypeError, ValueError):
                    days = 0
                if days:
                    self.db.execute("UPDATE preference_weights SET weight=?,updated_at=? WHERE dimension=? AND value=?",
                                    (float(row["weight"]) * (.995 ** days), now.isoformat(),
                                     row["dimension"], row["value"]))

    def list_digests(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute("""SELECT d.digest_date,d.overview,d.created_at,
          (SELECT count(*) FROM ratings r WHERE r.subject_type='item' AND r.digest_date=d.digest_date AND r.rating>=4) liked_count,
          (SELECT count(*) FROM digest_entries e WHERE e.digest_date=d.digest_date) item_count
          FROM digest_editions d ORDER BY d.digest_date DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_digest(self, digest_date: str) -> dict | None:
        edition = self.db.execute("SELECT * FROM digest_editions WHERE digest_date=?", (digest_date,)).fetchone()
        if not edition:
            return None
        entries = self.db.execute("""SELECT e.*,
          EXISTS(SELECT 1 FROM ratings r WHERE r.subject_type='item' AND r.digest_date=e.digest_date AND r.subject_id=e.item_id AND r.rating>=4) liked
          FROM digest_entries e WHERE e.digest_date=? ORDER BY position""", (digest_date,)).fetchall()
        result = dict(edition)
        result["items"] = [dict(row) for row in entries]
        for item in result["items"]:
            for key in ("links", "record_ids", "keywords", "tags"):
                item[key] = json.loads(item[key] or "[]")
        result["liked_count"] = sum(bool(item["liked"]) for item in result["items"])
        return result

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM daily_runs ORDER BY started_at DESC LIMIT ?", (limit,))]

    def list_logs(self, limit: int = 200, component: str | None = None) -> list[dict]:
        if component:
            rows = self.db.execute("SELECT * FROM job_logs WHERE component=? ORDER BY id DESC LIMIT ?", (component, limit))
        else:
            rows = self.db.execute("SELECT * FROM job_logs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]
