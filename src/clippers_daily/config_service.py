from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import load_yaml
from .storage import Store


SECTIONS = {
    "runtime": "runtime.yaml",
    "corporations": "coporations.yaml",
    "media": "media.yaml",
    "papers": "papers.yaml",
}


def validate_section(section: str, value: dict) -> None:
    if section not in SECTIONS:
        raise ValueError("未知配置区段")
    if not isinstance(value, dict) or int(value.get("version", 0)) < 1:
        raise ValueError("配置必须是包含 version 的对象")
    if section == "runtime":
        target = int(value.get("target_items", 0))
        if not 1 <= target <= 30:
            raise ValueError("target_items 必须在 1 到 30 之间")
        if int(value.get("minimum_deepseek_items", 0)) + int(value.get("minimum_zh_media_items", 0)) > target:
            raise ValueError("硬配额之和不能超过日报条数")
        for key in ("lookback_hours", "fallback_days", "minimum_deepseek_items", "minimum_zh_media_items",
                    "minimum_media_items", "minimum_paper_items"):
            if int(value.get(key, 0)) < 0:
                raise ValueError(f"{key} 不能为负数")
        email = value.get("email", {})
        senders = email.get("senders", [])
        sender_ids = [str(item.get("id", "")).strip() for item in senders]
        if any(not item for item in sender_ids) or len(sender_ids) != len(set(sender_ids)):
            raise ValueError("发件人 id 必须存在且唯一")
        if senders and email.get("active_sender") not in sender_ids:
            raise ValueError("默认发件人必须存在于发件配置中")
        provider_ids = []
        for provider in value.get("llm", {}).get("providers", []):
            provider_id = str(provider.get("id", "")).strip()
            if not provider_id or not provider.get("base_url") or not provider.get("model"):
                raise ValueError("模型 provider 必须包含 id、base_url 和 model")
            provider_ids.append(provider_id)
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("模型 provider id 必须唯一")
    collection_key = {"corporations": "corporations", "media": "sources", "papers": "sources"}.get(section)
    if collection_key:
        seen = set()
        for source in value.get(collection_key, []):
            source_id = str(source.get("id", "")).strip()
            if not source_id or source_id in seen:
                raise ValueError("信息源 id 必须存在且唯一")
            seen.add(source_id)
            if "channels" in source:
                channel_ids = [str(item.get("id", "")) for item in source.get("channels", [])]
                if any(not item for item in channel_ids) or len(channel_ids) != len(set(channel_ids)):
                    raise ValueError(f"{source_id} 的 channel id 必须存在且唯一")


class ConfigManager:
    def __init__(self, config_dir: Path, store: Store):
        self.config_dir = config_dir
        self.store = store

    def read(self, section: str) -> dict:
        if section not in SECTIONS:
            raise ValueError("未知配置区段")
        return load_yaml(self.config_dir / SECTIONS[section])

    def write(self, section: str, value: dict, actor: str = "admin") -> int:
        validate_section(section, value)
        path = self.config_dir / SECTIONS[section]
        previous = path.read_text(encoding="utf-8") if path.is_file() else ""
        content = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        with self.store.db:
            cursor = self.store.db.execute(
                "INSERT INTO config_revisions(section,content,previous_content,created_at,actor) VALUES (?,?,?,?,?)",
                (section, content, previous, datetime.now(timezone.utc).isoformat(), actor),
            )
            revision = int(cursor.lastrowid)
            self.store.db.execute(
                "INSERT INTO audit_events(happened_at,actor,action,target,detail) VALUES (?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), actor, "config.update", section, f'{{"revision":{revision}}}'),
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return revision

    def revisions(self, section: str, limit: int = 20) -> list[dict]:
        return [dict(row) for row in self.store.db.execute(
            "SELECT id,section,created_at,actor FROM config_revisions WHERE section=? ORDER BY id DESC LIMIT ?",
            (section, limit),
        )]

    def rollback(self, section: str, revision: int, actor: str = "admin") -> int:
        row = self.store.db.execute("SELECT content,previous_content FROM config_revisions WHERE id=? AND section=?", (revision, section)).fetchone()
        if not row:
            raise ValueError("配置版本不存在")
        value = yaml.safe_load(row["previous_content"] or row["content"]) or {}
        return self.write(section, value, actor)
