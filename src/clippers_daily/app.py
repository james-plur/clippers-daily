from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .collectors import collect_all, deduplicate
from .config import Settings
from .editor import build_digest
from .mailer import send_html
from .notes import publish_daily_inbox
from .render import write_digest
from .storage import Store
PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _publish_notes_best_effort(store: Store, run_id: str, markdown: str, digest, selected: list,
                               notes_config: dict) -> None:
    try:
        publish_daily_inbox(markdown, digest, selected, notes_config)
        store.log(run_id, "info", "git", "日报笔记同步完成", {"digest_date": digest.date})
    except Exception as exc:
        # Notes are an optional output adapter. A Git outage must not suppress
        # the primary email delivery.
        store.log(run_id, "warning", "git", "日报笔记同步失败，继续发送邮件", {
            "digest_date": digest.date, "error": str(exc)[:2000]})


def _rank(store: Store, records: list) -> list:
    return sorted(records, key=lambda r: (
        0 if r.source_id == "deepseek" else 1,
        0 if r.category == "media" and r.language.lower().startswith("zh") else 1,
        PRIORITY.get(r.priority, 9),
        -store.preference_score(r),
        -(r.published_at.timestamp() if r.published_at else 0),
    ))


def _candidate_pool(settings: Settings, store: Store, records: list, now: datetime) -> tuple[list, dict]:
    delivered_ids, delivered_urls, delivered_titles = store.delivered_identities()
    unseen = [r for r in records if r.parse_status == "complete" and r.id not in delivered_ids and r.url not in delivered_urls
              and Store.normalize_title(r.title) not in delivered_titles]
    fallback_cutoff = now - timedelta(days=settings.fallback_days)
    company_media = [r for r in unseen if r.category in {"company", "media"} and r.published_at and r.published_at >= fallback_cutoff]
    papers = [r for r in unseen if r.category == "paper" and r.published_at and r.published_at >= now - timedelta(days=365)]
    paper_keywords = {str(k).lower() for k in settings.papers.get("sources", [{}])[-1].get("selection", {}).get("topic_filter", {}).get("keywords", [])}
    def paper_score(record):
        text = f"{record.title} {record.summary} {record.metadata.get('keywords', '')}".lower()
        return (sum(1 for keyword in paper_keywords if keyword in text),
                float(record.metadata.get("relevance_score", 0) or 0),
                record.metadata.get("upvotes", 0), (record.published_at or fallback_cutoff).timestamp())
    papers.sort(key=paper_score, reverse=True)
    paperlab = [r for r in papers if r.source_id == "paperlab"]
    ranked = _rank(store, company_media)
    primary_cutoff = now - timedelta(hours=settings.lookback_hours)
    primary = [r for r in ranked if r.published_at >= primary_cutoff]
    fallback = [r for r in ranked if r.published_at < primary_cutoff]
    # Keep hard-priority candidates at the front, then provide enough breadth for the editor.
    eligible = primary + [r for r in fallback if r not in primary]
    deepseek = [r for r in eligible if r.source_id == "deepseek"]
    zh_media = [r for r in eligible if r.category == "media" and r.language.lower().startswith("zh")]
    media = [r for r in eligible if r.category == "media"]
    company = [r for r in eligible if r.category == "company"]
    ordered = []
    paperlab_quota = min(len(paperlab), settings.minimum_paperlab_items)
    for group in (deepseek[:10], zh_media[:20], paperlab[:paperlab_quota], media[:30], company[:60], papers[:40]):
        ordered.extend(r for r in group if r not in ordered)
    deepseek_quota = min(len(deepseek), settings.minimum_deepseek_items)
    zh_media_quota = min(len(zh_media), settings.minimum_zh_media_items)
    policy = {"minimum_deepseek_items": deepseek_quota,
              "minimum_zh_media_items": zh_media_quota,
              "minimum_paperlab_items": paperlab_quota,
              "reserved_record_ids": [r.id for r in deepseek[:deepseek_quota] + zh_media[:zh_media_quota]
                                      + paperlab[:paperlab_quota]]}
    return ordered, policy


def run_daily(settings: Settings, digest_date: date, send: bool = True, force_send: bool = False,
              mode: str | None = None):
    store = Store(settings.database)
    store.decay_preference_weights()
    run_id = datetime.now(timezone.utc).isoformat()
    run_mode = mode or ("send" if send else "preview")
    store.start_run(run_id, digest_date.isoformat(), run_mode)
    try:
        records, reports = collect_all(settings)
        store.save_records(records)
        store.save_reports(run_id, reports)
        historical = store.recent_records(datetime.now(timezone.utc) - timedelta(days=settings.fallback_days))
        records = deduplicate(records + historical)
        candidates, policy = _candidate_pool(settings, store, records, datetime.now(timezone.utc))
        store.log(run_id, "info", "selection", "候选池构建完成", {
            "records": len(records), "candidates": len(candidates),
            "deepseek_candidates": sum(r.source_id == "deepseek" for r in candidates),
            "zh_media_candidates": sum(r.category == "media" and r.language.lower().startswith("zh") for r in candidates),
            "policy": policy,
        })
        if not candidates:
            raise ValueError("去重和时间过滤后没有可推荐候选")
        for report in reports:
            report.eligible = sum(1 for record in candidates if record.source_id == report.source_id and
                                  (record.channel_id == report.channel_id or report.channel_id == "conference-catalog"))
        digest = build_digest(candidates, digest_date, settings.target_items, policy,
                              settings.runtime.get("llm", {}))
        selected_ids = {record_id for item in digest.items for record_id in item.record_ids}
        for report in reports:
            report.selected_for_digest = sum(1 for record in records if record.id in selected_ids and
                                              record.source_id == report.source_id and
                                              (record.channel_id == report.channel_id or report.channel_id == "conference-catalog"))
        store.save_reports(run_id, reports)
        for record in records:
            if record.id in selected_ids:
                record.metadata["digest_date"] = digest.date
        store.save_records(records)
        markdown_path, html_path = write_digest(digest, settings.output_dir)
        markdown = markdown_path.read_text(encoding="utf-8")
        html = html_path.read_text(encoding="utf-8")
        store.save_digest(digest, markdown, html, run_id)
        message_id = None
        selected = [r for r in records if r.id in selected_ids]
        if send:
            if store.was_delivered(digest.date) and not force_send:
                raise RuntimeError(f"{digest.date} 已发送；使用 --force-send 明确重发")
            subject = settings.runtime.get("email", {}).get("subject_template", "AI基础设施日报 - {date}").format(date=digest.date)
            notes_config = settings.runtime.get("notes", {})
            if notes_config.get("enabled", False):
                _publish_notes_best_effort(store, run_id, markdown, digest, selected, notes_config)
            message_id = send_html(subject, html_path, settings.runtime.get("email", {}))
            store.mark_delivered(digest.date, message_id, selected)
        store.finish_run(run_id, "success", str(markdown_path), str(html_path), message_id)
        return markdown_path, html_path, reports
    except Exception as exc:
        store.finish_run(run_id, "failed", error=str(exc)[:2000])
        raise
