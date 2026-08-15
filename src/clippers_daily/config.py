from __future__ import annotations

import os
from pathlib import Path

import yaml

PRIORITY_VALUES = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class Settings:
    def __init__(self, config_dir: Path | None = None, data_dir: Path | None = None):
        self.config_dir = config_dir or Path(os.getenv("CLIPPERS_CONFIG_DIR", "config"))
        self.runtime = load_yaml(self.config_dir / "runtime.yaml")
        self.corporations = load_yaml(self.config_dir / "coporations.yaml")
        self.media = load_yaml(self.config_dir / "media.yaml")
        self.papers = load_yaml(self.config_dir / "papers.yaml")
        code_path = self.config_dir / "code.yaml"
        self.code = load_yaml(code_path) if code_path.exists() else {"version": 1, "enabled": False}
        knowledge_path = self.config_dir / "knowledge.yaml"
        self.knowledge = load_yaml(knowledge_path) if knowledge_path.exists() else {}
        data_env = os.getenv("CLIPPERS_DATA_DIR")
        root = data_dir or Path(data_env or "data")
        self.database = Path(os.getenv("CLIPPERS_DATABASE", str(root / "clippers.db") if data_env or data_dir else self.runtime.get("database", root / "clippers.db")))
        self.output_dir = Path(os.getenv("CLIPPERS_OUTPUT_DIR", str(root / "reports") if data_env or data_dir else self.runtime.get("output_dir", root / "reports")))
        self.timezone = self.runtime.get("timezone", "Asia/Shanghai")
        self.target_items = int(self.runtime.get("target_items", 10))
        self.lookback_hours = int(self.runtime.get("lookback_hours", 36))
        self.fallback_days = int(self.runtime.get("fallback_days", 7))
        self.minimum_deepseek_items = int(self.runtime.get("minimum_deepseek_items", 1))
        self.minimum_zh_media_items = int(self.runtime.get("minimum_zh_media_items", 1))
        self.minimum_media_items = int(self.runtime.get("minimum_media_items", 1))
        self.minimum_paper_items = int(self.runtime.get("minimum_paper_items", 1))
        self.minimum_paperlab_items = int(self.runtime.get("minimum_paperlab_items", 1))
        self.maximum_items = int(self.runtime.get("maximum_items", 15))

    def source(self, source_id: str) -> dict | None:
        for section, key in ((self.corporations, "corporations"), (self.media, "sources"), (self.papers, "sources")):
            for source in section.get(key, []):
                if source.get("id") == source_id:
                    return source
        return None
