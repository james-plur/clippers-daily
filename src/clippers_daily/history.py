from __future__ import annotations

import re

from .models import Digest, DigestItem


CATEGORIES = {"企业": "company", "媒体": "media", "论文": "paper"}


def parse_digest_markdown(markdown: str, digest_date: str) -> Digest:
    """Parse both the legacy and current rendered daily Markdown formats."""
    overview_match = re.search(
        r"^# AI 基础设施日报[^\n]*\n\s*(.*?)\n\s*## 今日速览",
        markdown, re.MULTILINE | re.DOTALL)
    overview = overview_match.group(1).strip() if overview_match else "历史日报"
    summary_categories = {
        int(position): CATEGORIES.get(label, "company")
        for position, label in re.findall(
            r"^(\d+)\.\s+\*\*\[([^\]]+)\]", markdown, re.MULTILINE)
    }
    details = markdown.split("## 详情", 1)[-1]
    sections = re.findall(
        r"^###\s+(\d+)\.\s+(.+?)\s*$\n(.*?)(?=^###\s+\d+\.|\Z)",
        details, re.MULTILINE | re.DOTALL)
    items: list[DigestItem] = []
    for position_text, title, body in sections:
        position = int(position_text)
        source_match = re.search(r"^- 来源：\s*(.+?)\s*$", body, re.MULTILINE)
        reason_match = re.search(r"^- 收录原因：\s*(.+?)\s*$", body, re.MULTILINE)
        keywords_match = re.search(r"^- 关键词：\s*(.*?)\s*$", body, re.MULTILINE)
        tags_match = re.search(r"^- 标签：\s*(.*?)\s*$", body, re.MULTILINE)
        links = re.findall(r"\[[^\]]*\]\((https?://[^)]+)\)", body)
        detail_lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("- 来源：", "- 收录原因：", "- 关键词：", "- 标签：", "链接：")):
                continue
            if stripped:
                detail_lines.append(stripped)
        keywords = [value.strip() for value in re.split(r"[、,，]", keywords_match.group(1)) if value.strip()] if keywords_match else []
        tags = re.findall(r"#([^\s#]+)", tags_match.group(1)) if tags_match else []
        seed = links or [f"legacy:{digest_date}:{position}"]
        items.append(DigestItem(
            record_ids=seed, title=title.strip(),
            source=source_match.group(1).strip() if source_match else "历史日报",
            reason=reason_match.group(1).strip() if reason_match else "历史日报回填",
            detail="\n".join(detail_lines).strip(), links=links,
            category=summary_categories.get(position, "company"),
            keywords=keywords, tags=tags))
    if not items:
        raise ValueError(f"{digest_date} 未解析到日报条目")
    return Digest(date=digest_date, overview=overview, items=items)
