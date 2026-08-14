from __future__ import annotations

from html import escape
from pathlib import Path

from .models import Digest


LABELS = {"company": "企业", "media": "媒体", "paper": "论文"}


def render_markdown(digest: Digest) -> str:
    lines = [
        "---",
        f'title: "AI 基础设施日报 · {digest.date}"',
        f"date: {digest.date}",
        "type: daily",
        "tags:",
        "  - clippers/日报",
        "  - AI基础设施",
        "aliases:",
        f'  - "{digest.date} AI 日报"',
        "---",
        "",
        f"# AI 基础设施日报 · {digest.date}", "", digest.overview, "", "## 今日速览", "",
    ]
    for index, item in enumerate(digest.items, 1):
        lines.append(f"{index}. **[{LABELS[item.category]}] {item.title}** — {item.reason}")
    lines += ["", "## 详情", ""]
    for index, item in enumerate(digest.items, 1):
        lines += [f"### {index}. {item.title}", "", f"- 来源：{item.source}", f"- 收录原因：{item.reason}",
                  f"- 关键词：{'、'.join(item.keywords)}", f"- 标签：{' '.join('#' + tag for tag in item.tags)}", "",
                  item.detail, "", "链接：" + "、".join(f"[{url}]({url})" for url in item.links), ""]
    return "\n".join(lines).rstrip() + "\n"


def render_html(digest: Digest) -> str:
    overview = escape(digest.overview)
    summary_rows = "".join(
        f'<tr><td>{i}</td><td><span class="tag">{LABELS[x.category]}</span></td><td><a href="{escape(x.links[0], quote=True)}">{escape(x.title)}</a></td><td>{escape(x.reason)}</td></tr>'
        for i, x in enumerate(digest.items, 1)
    )
    details = "".join(
        f'<section><h2>{i}. {escape(x.title)}</h2><p class="meta"><b>来源：</b>{escape(x.source)}<br><b>收录原因：</b>{escape(x.reason)}</p><p>{escape(x.detail)}</p><p>'
        + " · ".join(f'<a href="{escape(url, quote=True)}">原文链接</a>' for url in x.links) + "</p></section>"
        for i, x in enumerate(digest.items, 1)
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>
body{{margin:0;background:#f6f8fa;color:#1f2328;font:15px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}main{{max-width:760px;margin:auto;background:#fff;padding:28px}}h1{{font-size:26px}}h2{{font-size:19px}}table{{border-collapse:collapse;width:100%}}td{{padding:8px;border-bottom:1px solid #d0d7de;vertical-align:top}}a{{color:#0969da}}.tag{{background:#ddf4ff;padding:2px 6px;border-radius:4px}}.meta{{background:#f6f8fa;padding:10px}}section{{border-top:1px solid #d0d7de;margin-top:24px}}
</style></head><body><main><h1>AI 基础设施日报 · {escape(digest.date)}</h1><p>{overview}</p><h2>今日速览</h2><table>{summary_rows}</table><h2>详情</h2>{details}</main></body></html>"""


def write_digest(digest: Digest, output_root: Path) -> tuple[Path, Path]:
    directory = output_root / digest.date
    directory.mkdir(parents=True, exist_ok=True)
    markdown_path, html_path = directory / "daily.md", directory / "daily.html"
    markdown_path.write_text(render_markdown(digest), encoding="utf-8")
    html_path.write_text(render_html(digest), encoding="utf-8")
    return markdown_path, html_path
