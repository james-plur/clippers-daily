from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


Category = Literal["company", "media", "paper"]


class Record(BaseModel):
    id: str
    category: Category
    title: str
    url: str
    source_name: str
    source_id: str
    channel_id: str
    published_at: datetime | None = None
    collected_at: datetime
    summary: str = ""
    priority: str = "P2"
    language: str = "auto"
    discovered_at: datetime | None = None
    source_priority: int = 2
    parse_status: str = "complete"
    topics: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class RunReport(BaseModel):
    source_id: str
    channel_id: str
    status: str
    fetched: int = 0
    parsed: int = 0
    selected: int = 0
    duplicate: int = 0
    filtered: int = 0
    eligible: int = 0
    selected_for_digest: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class DigestItem(BaseModel):
    record_ids: list[str]
    title: str
    source: str
    reason: str
    detail: str
    links: list[str]
    category: Category
    keywords: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Digest(BaseModel):
    date: str
    overview: str
    items: list[DigestItem]
