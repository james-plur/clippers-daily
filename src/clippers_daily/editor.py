from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

from .collectors import canonicalize
from .llm import json_completion
from .models import Digest, Record


SYSTEM = """你是 Clippers 的 AI 基础设施日报编辑。只使用提供的候选记录，不得补写候选中没有的事实。
输出中文；专业、准确、克制。优先时效性、技术深度、来源权威性和 AI 基础设施相关性。
合并同一事件的多来源报道。相同会议集中发布的论文应汇总为一个条目。
候选充足时必须达到目标条数。只要候选中存在相应类别，就必须包含媒体和论文。重要代码更新优先，每个 Star 仓库必须独立成条；关注组织汇总可以合并为一个条目。媒体条目必须来自category=media的独立媒体候选，不得把企业博客冒充媒体。
每条详情目标约500个中文字符，300至600字均可接受。详情应具体说明事件背景、关键事实或技术机制、量化结果（候选未提供则明确未披露）、影响范围、局限性以及为什么值得关注；不得用“以原始链接为准”等空泛句子凑字数。
每个条目给出 3 至 8 个关键词和 2 至 6 个适合 Obsidian 的中文标签；标签不要包含空格或 #。"""


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["date", "overview", "items"],
    "properties": {
        "date": {"type": "string"},
        "overview": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["record_ids", "title", "source", "reason", "detail", "links", "category", "keywords", "tags"],
                "properties": {
                    "record_ids": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": "string"}, "source": {"type": "string"},
                    "reason": {"type": "string"}, "detail": {"type": "string"},
                    "links": {"type": "array", "items": {"type": "string"}},
                    "category": {"type": "string", "enum": ["company", "media", "paper", "code"]},
                    "keywords": {"type": "array", "minItems": 3, "maxItems": 8, "items": {"type": "string"}},
                    "tags": {"type": "array", "minItems": 2, "maxItems": 6, "items": {"type": "string"}},
                },
            },
        },
    },
}


def build_digest(records: list[Record], digest_date: date, target: int, policy: dict | None = None,
                 llm_config: dict | None = None) -> Digest:
    if not records:
        raise ValueError("没有可用于生成日报的新候选记录")
    aliases = {f"c{index:03d}": record.id for index, record in enumerate(records[:120], 1)}
    reverse_aliases = {record_id: alias for alias, record_id in aliases.items()}
    candidates = []
    for alias, record in zip(aliases, records[:120]):
        metadata = {key: value for key, value in record.metadata.items()
                    if key not in {"paperlab_id", "id", "record_id", "sync_id"}}
        candidates.append({
            "id": alias, "category": record.category, "title": record.title, "url": record.url,
            "source_name": record.source_name, "source_id": record.source_id,
            "channel_id": record.channel_id,
            "published_at": record.published_at.isoformat() if record.published_at else None,
            "summary": record.summary, "language": record.language, "topics": record.topics,
            "metadata": metadata,
        })
    policy = policy or {}
    requirements = []
    deepseek_quota = int(policy.get("minimum_deepseek_items", policy.get("require_deepseek", 0)))
    zh_media_quota = int(policy.get("minimum_zh_media_items", policy.get("require_zh_media", 0)))
    paperlab_quota = int(policy.get("minimum_paperlab_items", 0))
    code_quota = int(policy.get("minimum_important_code_items", 0))
    if deepseek_quota:
        requirements.append(f"至少 {deepseek_quota} 个条目必须引用 source_id=deepseek 的记录")
    if zh_media_quota:
        requirements.append(f"至少 {zh_media_quota} 个条目必须引用 language=zh-CN 且 category=media 的记录")
    if paperlab_quota:
        requirements.append(f"至少 {paperlab_quota} 个条目必须引用 source_id=paperlab 的记录")
    if code_quota:
        requirements.append(f"至少 {code_quota} 个条目必须引用 metadata.important_code=true 的代码记录，每个仓库单独成条")
    if policy.get("reserved_record_ids"):
        required_aliases = [reverse_aliases[item] for item in policy["reserved_record_ids"] if item in reverse_aliases]
        requirements.append("必须引用这些确定性保底候选：" + ", ".join(required_aliases))
    prompt = (
        f"为 {digest_date.isoformat()} 生成日报，目标 {target} 条；候选数不少于目标时必须恰好输出 {target} 条，候选不足时宁缺毋滥。"
        + ("硬性约束：" + "；".join(requirements) + "。" if requirements else "")
        + "只能引用给定 record id 和 URL。严格返回符合下述 JSON Schema 的 JSON 对象，不要输出 Markdown 代码围栏。\n"
        f"JSON Schema:\n{json.dumps(SCHEMA, ensure_ascii=False)}\n候选数据：\n{json.dumps(candidates, ensure_ascii=False)}"
    )
    repair_hint = Path(os.getenv("CLIPPERS_DATA_DIR", "data")) / "digest_repair_prompt.txt"
    if repair_hint.is_file():
        prompt += "\n自动修复补充指令（不得覆盖事实、链接和硬配额）：\n" + repair_hint.read_text(encoding="utf-8")[:4000]
    error = None
    for attempt in range(5):
        retry = "" if not error else f"\n上次输出未通过校验：{error}。请重新生成完整JSON，重点修正该问题。"
        content = json_completion(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt + retry}],
            max_tokens=16384, config=llm_config,
        )
        if not content:
            error = ValueError("智谱模型返回空内容")
            continue
        try:
            digest = Digest.model_validate_json(content)
            for item in digest.items:
                item.record_ids = [aliases.get(record_id, record_id) for record_id in item.record_ids]
            return _validate_digest(digest, records, target, policy)
        except (ValueError, TypeError) as exc:
            error = exc
    raise ValueError(f"日报连续5次未通过质量校验: {error}")


def _validate_digest(digest: Digest, records: list[Record], target: int, policy: dict | None = None) -> Digest:
    policy = policy or {}
    allowed = {r.id: r for r in records}
    url_to_id = {}
    for record in records:
        urls = record.source_urls + [record.url]
        if record.metadata.get("doi"):
            urls.append(f"https://doi.org/{record.metadata['doi']}")
        for url in urls:
            url_to_id[canonicalize(url).lower()] = record.id
    for item in digest.items:
        if not item.record_ids or any(record_id not in allowed for record_id in item.record_ids):
            raise ValueError(f"模型返回未知记录: {item.record_ids}")
        for link in item.links:
            linked_record_id = url_to_id.get(canonicalize(link).lower())
            if linked_record_id and linked_record_id not in item.record_ids:
                item.record_ids.append(linked_record_id)
        if not item.links or any(url_to_id.get(canonicalize(link).lower()) not in item.record_ids for link in item.links):
            raise ValueError(f"模型返回未经候选支持的链接: {item.links}")
        item.keywords = list(dict.fromkeys(x.strip() for x in item.keywords if x.strip()))[:8]
        item.tags = list(dict.fromkeys(x.strip().lstrip("#").replace(" ", "-") for x in item.tags if x.strip()))[:6]
        if len(item.keywords) < 3 or len(item.tags) < 2:
            raise ValueError(f"关键词或标签数量不足: {item.title}")
        length = len("".join(item.detail.split()))
        if not 200 <= length <= 800:
            logging.warning("条目详情长度偏离建议范围(%d): %s", length, item.title)
    expected = min(target, len(records))
    if len(digest.items) != expected:
        raise ValueError(f"模型返回 {len(digest.items)} 条，期望 {expected} 条")
    available_categories = {r.category for r in records}
    selected_categories = {item.category for item in digest.items}
    for required in {"media", "paper"} & available_categories:
        if required not in selected_categories:
            raise ValueError(f"候选中存在 {required}，但模型未选择该类别")
    selected_ids = {record_id for item in digest.items for record_id in item.record_ids}
    deepseek_quota = int(policy.get("minimum_deepseek_items", policy.get("require_deepseek", 0)))
    zh_media_quota = int(policy.get("minimum_zh_media_items", policy.get("require_zh_media", 0)))
    paperlab_quota = int(policy.get("minimum_paperlab_items", 0))
    code_quota = int(policy.get("minimum_important_code_items", 0))
    selected_deepseek_items = sum(any(allowed[record_id].source_id == "deepseek" for record_id in item.record_ids)
                                 for item in digest.items)
    selected_zh_items = sum(any(allowed[record_id].category == "media" and
                                allowed[record_id].language.lower().startswith("zh") for record_id in item.record_ids)
                            for item in digest.items)
    selected_paperlab_items = sum(any(allowed[record_id].source_id == "paperlab" for record_id in item.record_ids)
                                  for item in digest.items)
    selected_code_items = sum(any(allowed[record_id].category == "code" and allowed[record_id].metadata.get("important_code")
                                  for record_id in item.record_ids) for item in digest.items)
    if selected_deepseek_items < deepseek_quota:
        raise ValueError(f"DeepSeek 条目不足：需要 {deepseek_quota}，实际 {selected_deepseek_items}")
    if selected_zh_items < zh_media_quota:
        raise ValueError(f"中文媒体条目不足：需要 {zh_media_quota}，实际 {selected_zh_items}")
    if selected_paperlab_items < paperlab_quota:
        raise ValueError(f"PaperLab 条目不足：需要 {paperlab_quota}，实际 {selected_paperlab_items}")
    if selected_code_items < code_quota:
        raise ValueError(f"重要代码条目不足：需要 {code_quota}，实际 {selected_code_items}")
    missing_reserved = set(policy.get("reserved_record_ids", [])) - selected_ids
    if missing_reserved:
        raise ValueError(f"未选择确定性保底候选: {sorted(missing_reserved)}")
    return digest
