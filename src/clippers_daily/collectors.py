from __future__ import annotations

import hashlib
import gzip
import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from .models import Record, RunReport


TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref"}
PRIORITY_VALUES = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def canonicalize(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in TRACKING])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", query, ""))


def stable_id(kind: str, url: str, extra: str = "") -> str:
    return f"{kind}:" + hashlib.sha256(f"{canonicalize(url)}|{extra}".encode()).hexdigest()


def normalize_sitemap_url(value: str) -> str:
    """Repair malformed sitemap loc values that contain a second absolute URL."""
    value = value.strip()
    matches = list(re.finditer(r"https?://", value, re.I))
    if len(matches) > 1:
        value = value[matches[-1].start():]
    return canonicalize(value)


def date_from_path(url: str, pattern: str | None = None) -> datetime | None:
    path = urlsplit(url).path
    match = re.search(pattern, path) if pattern else None
    if match:
        try:
            return datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            pass
    news = re.search(r"/news/news(\d{2})(\d{2})(\d{2})$", path)
    if news:
        year, month, day = map(int, news.groups())
        try:
            return datetime(2000 + year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Collector:
    def __init__(self, timeout: int = 30):
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Clippers/0.1"})

    def get_with_backoff(self, url: str, **kwargs) -> httpx.Response:
        response = None
        for attempt in range(3):
            response = self.client.get(url, **kwargs)
            if response.status_code not in {429, 503}:
                return response
            if attempt < 2:
                time.sleep(2 ** attempt)
        return response

    def feed(self, category, owner_id, owner_name, channel) -> tuple[list[Record], RunReport]:
        endpoint = channel["endpoint"]
        try:
            response = self.client.get(endpoint)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError(str(parsed.bozo_exception))
            records = []
            keywords = [str(value).lower() for value in channel.get("include_keywords", [])]
            filtered = 0
            for entry in parsed.entries:
                url = canonicalize(entry.get("link", endpoint))
                published = parse_time(entry.get("published") or entry.get("updated"))
                raw_title = str(entry.get("title", ""))
                raw_summary = str(entry.get("summary", ""))
                title = BeautifulSoup(raw_title, "html.parser").get_text(" ", strip=True) if "<" in raw_title else raw_title.strip()
                summary = BeautifulSoup(raw_summary, "html.parser").get_text(" ", strip=True) if "<" in raw_summary else raw_summary.strip()
                if keywords and not any(keyword in f"{title} {summary}".lower() for keyword in keywords):
                    filtered += 1
                    continue
                records.append(Record(
                    id=stable_id(category, url, entry.get("id", "")), category=category,
                    title=title,
                    url=url, source_name=owner_name, source_id=owner_id,
                    channel_id=channel["id"], published_at=published,
                    collected_at=datetime.now(timezone.utc),
                    summary=summary[:2000],
                    priority=channel.get("priority", "P2"), source_urls=[url],
                ))
            return records, RunReport(source_id=owner_id, channel_id=channel["id"], status="success",
                                      fetched=len(parsed.entries), selected=len(records), filtered=filtered)
        except Exception as exc:
            return [], RunReport(source_id=owner_id, channel_id=channel["id"], status="fetch_error", error=str(exc)[:500])

    def sitemap(self, category, owner_id, owner_name, channel) -> tuple[list[Record], RunReport]:
        endpoint = channel["endpoint"]
        try:
            response = self.client.get(endpoint)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "xml")
            records = []
            nodes = soup.find_all("url")
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(channel.get("detail_lookback_days", 14)))
            for node in nodes:
                loc = node.find("loc")
                if not loc or (channel.get("url_filter") and channel["url_filter"] not in loc.text):
                    continue
                url = normalize_sitemap_url(loc.text)
                modified = parse_time(node.find("lastmod").text if node.find("lastmod") else None)
                published = date_from_path(url, channel.get("article_date_pattern")) or modified
                title = urlsplit(url).path.rstrip("/").split("/")[-1].replace("-", " ")
                summary = ""
                canonical_url = url
                parse_status = "partial"
                if published and published >= cutoff:
                    try:
                        detail = self.client.get(url)
                        detail.raise_for_status()
                        page = BeautifulSoup(detail.text, "html.parser")
                        title_node = page.select_one("meta[property='og:title']") or page.select_one("h1") or page.select_one("title")
                        if title_node:
                            title = (title_node.get("content") or title_node.get_text(" ", strip=True)).strip()
                        description = page.select_one("meta[name='description']") or page.select_one("meta[property='og:description']")
                        summary = (description.get("content", "").strip() if description else "")[:2000]
                        canonical_node = page.select_one("link[rel='canonical']")
                        if canonical_node and canonical_node.get("href"):
                            canonical_url = normalize_sitemap_url(urljoin(url, canonical_node["href"]))
                        parse_status = "complete" if title and title.lower() not in {"news", "deepseek"} else "partial"
                    except Exception:
                        pass
                records.append(Record(id=stable_id(category, canonical_url), category=category,
                    title=title, url=canonical_url,
                    source_name=owner_name, source_id=owner_id, channel_id=channel["id"],
                    published_at=published, discovered_at=datetime.now(timezone.utc), collected_at=datetime.now(timezone.utc),
                    summary=summary, priority=channel.get("priority", "P2"), parse_status=parse_status,
                    source_priority=PRIORITY_VALUES.get(channel.get("priority", "P2"), 2), source_urls=[canonical_url],
                    metadata={"published_at_source": "url" if date_from_path(url, channel.get("article_date_pattern")) else "sitemap"}))
            return records, RunReport(source_id=owner_id, channel_id=channel["id"], status="success", fetched=len(records), selected=len(records))
        except Exception as exc:
            return [], RunReport(source_id=owner_id, channel_id=channel["id"], status="fetch_error", error=str(exc)[:500])

    def html_listing(self, category, owner_id, owner_name, channel) -> tuple[list[Record], RunReport]:
        endpoint = channel["endpoint"]
        try:
            response = self.client.get(endpoint)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            records, seen = [], set()
            for link in soup.select("a[href]"):
                title = link.get_text(" ", strip=True)
                url = canonicalize(urljoin(endpoint, link["href"]))
                if len(title) < 12 or url in seen or urlsplit(url).netloc != urlsplit(endpoint).netloc:
                    continue
                seen.add(url)
                records.append(Record(id=stable_id(category, url), category=category, title=title,
                    url=url, source_name=owner_name, source_id=owner_id, channel_id=channel["id"],
                    collected_at=datetime.now(timezone.utc), priority=channel.get("priority", "P2"), source_urls=[url]))
            return records[:50], RunReport(source_id=owner_id, channel_id=channel["id"], status="success", fetched=len(records), selected=min(50, len(records)))
        except Exception as exc:
            return [], RunReport(source_id=owner_id, channel_id=channel["id"], status="fetch_error", error=str(exc)[:500])

    def gzip_sitemap(self, source: dict, channel: dict) -> tuple[list[Record], RunReport]:
        """Collect recent articles from an official gzip sitemap."""
        try:
            response = self.client.get(channel["endpoint"])
            response.raise_for_status()
            content = response.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)
            soup = BeautifulSoup(content, "xml")
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(channel.get("lookback_days", 14)))
            records = []
            for node in soup.find_all("url"):
                loc = node.find("loc")
                if not loc:
                    continue
                url = normalize_sitemap_url(loc.text)
                if channel.get("url_filter") and channel["url_filter"] not in url:
                    continue
                published = date_from_path(url, channel.get("article_date_pattern"))
                if not published or published < cutoff:
                    continue
                title = urlsplit(url).path.rstrip("/").split("/")[-1].replace("-", " ")
                summary = ""
                parse_status = "partial"
                canonical_url = url
                try:
                    detail = self.client.get(url)
                    detail.raise_for_status()
                    page = BeautifulSoup(detail.text, "html.parser")
                    title_node = page.select_one("meta[property='og:title']") or page.select_one("h1") or page.select_one("title")
                    if title_node:
                        title = (title_node.get("content") or title_node.get_text(" ", strip=True)).strip()
                    description = page.select_one("meta[name='description']") or page.select_one("meta[property='og:description']")
                    summary = description.get("content", "").strip()[:2000] if description else ""
                    canonical_node = page.select_one("link[rel='canonical']")
                    if canonical_node and canonical_node.get("href"):
                        canonical_url = normalize_sitemap_url(urljoin(url, canonical_node["href"]))
                    parse_status = "complete" if title and not any(
                        marker in title for marker in ("数据服务", "费劲 爬数据", "费劲爬数据")) else "partial"
                except Exception:
                    pass
                if parse_status != "complete":
                    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
                    title = f"机器之心文章 {slug}（元数据待恢复）"
                    summary = "官方 sitemap 已发现该文章，但原站当前返回数据服务占位页，未将占位文案作为文章标题。"
                records.append(Record(
                    id=stable_id("media", canonical_url), category="media", title=title, url=canonical_url,
                    source_name=source["name"], source_id=source["id"], channel_id=channel["id"],
                    published_at=published, discovered_at=datetime.now(timezone.utc), collected_at=datetime.now(timezone.utc),
                    summary=summary, priority=channel.get("priority", source.get("priority", "P1")),
                    language=source.get("language", "auto"), source_priority=PRIORITY_VALUES.get(source.get("priority", "P1"), 1),
                    parse_status=parse_status, source_urls=[canonical_url], metadata={"published_at_source": "article_url",
                    "claim_status": "primary_source", "metadata_available": parse_status == "complete"},
                ))
            records.sort(key=lambda item: item.published_at or cutoff, reverse=True)
            complete = sum(record.parse_status == "complete" for record in records)
            degraded = bool(records) and complete == 0
            return records, RunReport(
                source_id=source["id"], channel_id=channel["id"],
                status="degraded" if degraded else "success",
                fetched=len(soup.find_all("url")), parsed=complete, selected=len(records),
                filtered=max(0, len(soup.find_all("url")) - len(records)),
                error=("官方 sitemap 正常，但近期文章页全部返回数据服务占位内容；候选已保留为 partial，"
                       "不会进入日报。" if degraded else None))
        except Exception as exc:
            return [], RunReport(source_id=source["id"], channel_id=channel["id"], status="fetch_error", error=str(exc)[:500])

    def github_org(self, category: str, owner_id: str, owner_name: str, channel: dict) -> tuple[list[Record], RunReport]:
        try:
            org = urlsplit(channel["endpoint"]).path.strip("/").split("/")[0]
            headers = {"Accept": "application/vnd.github+json"}
            if os.getenv("GITHUB_TOKEN"):
                headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
            response = self.client.get(f"https://api.github.com/orgs/{org}/repos",
                                       params={"sort": "pushed", "direction": "desc", "per_page": 50}, headers=headers)
            if response.status_code in {403, 429}:
                reset = response.headers.get("x-ratelimit-reset", "unknown")
                return [], RunReport(source_id=owner_id, channel_id=channel["id"], status="rate_limited",
                                     error=f"GitHub API rate limit; reset={reset}")
            response.raise_for_status()
            repos = response.json()
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(channel.get("lookback_days", 14)))
            records: list[Record] = []
            for repo in repos[:30]:
                created = parse_time(repo.get("created_at"))
                updated = parse_time(repo.get("pushed_at") or repo.get("updated_at"))
                event_time = created if created and created >= cutoff else updated
                if event_time and event_time >= cutoff:
                    url = canonicalize(repo["html_url"])
                    records.append(Record(id=stable_id(category, url), category=category,
                        title=f"{repo['full_name']} {'开源发布' if created and created >= cutoff else '仓库更新'}", url=url, source_name=owner_name,
                        source_id=owner_id, channel_id=channel["id"], published_at=event_time,
                        discovered_at=datetime.now(timezone.utc), collected_at=datetime.now(timezone.utc),
                        summary=(repo.get("description") or "")[:2000], priority=channel.get("priority", "P1"),
                        source_priority=PRIORITY_VALUES.get(channel.get("priority", "P1"), 1), source_urls=[url],
                        metadata={"github_kind": "repository", "created_at": repo.get("created_at"),
                                  "updated_at": repo.get("updated_at"), "stars": repo.get("stargazers_count", 0)}))
                releases = self.client.get(repo["releases_url"].replace("{/id}", ""), params={"per_page": 5}, headers=headers)
                if releases.status_code != 200:
                    continue
                for release in releases.json():
                    published = parse_time(release.get("published_at") or release.get("created_at"))
                    if not published or published < cutoff:
                        continue
                    url = canonicalize(release.get("html_url") or repo["html_url"])
                    records.append(Record(id=stable_id(category, url), category=category,
                        title=f"{repo['full_name']} {release.get('name') or release.get('tag_name') or 'Release'}", url=url,
                        source_name=owner_name, source_id=owner_id, channel_id=channel["id"], published_at=published,
                        discovered_at=datetime.now(timezone.utc), collected_at=datetime.now(timezone.utc),
                        summary=(release.get("body") or repo.get("description") or "")[:2000],
                        priority=channel.get("priority", "P1"), source_priority=PRIORITY_VALUES.get(channel.get("priority", "P1"), 1),
                        source_urls=[url], metadata={"github_kind": "release", "repository": repo["full_name"]}))
            return records, RunReport(source_id=owner_id, channel_id=channel["id"], status="success",
                                      fetched=len(repos), parsed=len(repos), selected=len(records))
        except Exception as exc:
            return [], RunReport(source_id=owner_id, channel_id=channel["id"], status="fetch_error", error=str(exc)[:500])

    def huggingface_org(self, category: str, owner_id: str, owner_name: str, channel: dict) -> tuple[list[Record], RunReport]:
        try:
            author = urlsplit(channel["endpoint"]).path.strip("/").split("/")[0]
            headers = {"Authorization": f"Bearer {os.environ['HF_TOKEN']}"} if os.getenv("HF_TOKEN") else {}
            responses = []
            for kind in ("models", "datasets"):
                response = self.client.get(f"https://huggingface.co/api/{kind}",
                                           params={"author": author, "sort": "lastModified", "direction": -1, "limit": 50},
                                           headers=headers)
                if response.status_code == 429:
                    return [], RunReport(source_id=owner_id, channel_id=channel["id"], status="rate_limited",
                                         error="Hugging Face API rate limit")
                response.raise_for_status()
                responses.append((kind[:-1], response.json()))
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(channel.get("lookback_days", 14)))
            records = []
            fetched = 0
            for kind, data in responses:
                fetched += len(data)
                for item in data:
                    modified = parse_time(item.get("lastModified"))
                    if not modified or modified < cutoff:
                        continue
                    item_id = item.get("modelId") or item.get("id")
                    if not item_id:
                        continue
                    prefix = "datasets/" if kind == "dataset" else ""
                    url = canonicalize(f"https://huggingface.co/{prefix}{item_id}")
                    label = "数据集" if kind == "dataset" else "模型"
                    records.append(Record(id=stable_id(category, url), category=category,
                        title=f"{item_id} {label}更新", url=url, source_name=owner_name, source_id=owner_id,
                        channel_id=channel["id"], published_at=modified, discovered_at=datetime.now(timezone.utc),
                        collected_at=datetime.now(timezone.utc), summary="、".join(item.get("tags") or [])[:2000],
                        priority=channel.get("priority", "P1"), source_priority=PRIORITY_VALUES.get(channel.get("priority", "P1"), 1),
                        source_urls=[url], metadata={"huggingface_kind": kind, "downloads": item.get("downloads", 0)}))
            return records, RunReport(source_id=owner_id, channel_id=channel["id"], status="success",
                                      fetched=fetched, parsed=fetched, selected=len(records))
        except Exception as exc:
            return [], RunReport(source_id=owner_id, channel_id=channel["id"], status="fetch_error", error=str(exc)[:500])

    def hf_papers(self, source: dict) -> tuple[list[Record], RunReport]:
        try:
            limit = source.get("request", {}).get("limit", 100)
            response = self.client.get(source["endpoint"], params={"limit": limit})
            response.raise_for_status()
            data = response.json()
            rules = source.get("selection", {})
            cutoff = datetime.now(timezone.utc) - timedelta(days=rules.get("lookback_days", 7))
            keywords = [x.lower() for x in rules.get("topic_filter", {}).get("keywords", [])]
            records = []
            for item in data:
                paper = item.get("paper", item)
                arxiv_id = paper.get("id")
                title = paper.get("title") or item.get("title", "")
                discovered = parse_time(paper.get("submittedOnDailyAt") or item.get("publishedAt"))
                text = f"{title} {paper.get('summary', '')}".lower()
                if not arxiv_id or not title or (discovered and discovered < cutoff):
                    continue
                if keywords and not any(k in text for k in keywords):
                    continue
                url = f"https://arxiv.org/abs/{arxiv_id}"
                records.append(Record(id=f"arxiv:{arxiv_id}", category="paper", title=title,
                    url=url, source_name=source["name"], source_id=source["id"], channel_id="daily-papers",
                    published_at=parse_time(paper.get("publishedAt")), collected_at=datetime.now(timezone.utc),
                    summary=paper.get("summary", "")[:2000], priority=source.get("priority", "P1"),
                    source_urls=[url], metadata={"upvotes": paper.get("upvotes", 0),
                    "comments": item.get("numComments", 0),
                    "discovered_at": discovered.isoformat() if discovered else None}))
            records.sort(key=lambda r: (r.metadata.get("upvotes", 0), r.metadata.get("comments", 0)), reverse=True)
            top = records[:rules.get("daily_top_n", 20)]
            return top, RunReport(source_id=source["id"], channel_id="daily-papers", status="success", fetched=len(data), selected=len(top), filtered=len(data)-len(top))
        except Exception as exc:
            return [], RunReport(source_id=source["id"], channel_id="daily-papers", status="fetch_error", error=str(exc)[:500])

    def zhihu_search(self, source: dict, channel: dict) -> tuple[list[Record], RunReport]:
        token = os.getenv("ZHIHU_ACCESS_TOKEN", "").strip()
        if not token:
            return [], RunReport(source_id=source["id"], channel_id=channel["id"], status="auth_error", error="ZHIHU_ACCESS_TOKEN is not configured")
        try:
            base_query = channel.get("query", source["name"])
            queries = [base_query, f"{base_query} {datetime.now().year}"]
            items = []
            for query in queries:
                response = self.get_with_backoff(
                    os.getenv("ZHIHU_SEARCH_URL", "https://developer.zhihu.com/api/v1/content/zhihu_search"),
                    params={"Query": query, "Count": channel.get("count", 20)},
                    headers={"Authorization": f"Bearer {token}", "X-Request-Timestamp": str(int(datetime.now().timestamp()))},
                )
                if response.status_code == 429:
                    return [], RunReport(source_id=source["id"], channel_id=channel["id"], status="rate_limited",
                                         error="Zhihu search rate limit after retries")
                response.raise_for_status()
                payload = response.json()
                if payload.get("Code") != 0:
                    raise ValueError(f"Zhihu API code={payload.get('Code')}: {payload.get('Message')}")
                items.extend(payload.get("Data", {}).get("Items", []))
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=7)).date()
            records, seen = [], set()
            for item in items:
                published = datetime.fromtimestamp(item.get("EditTime", 0), timezone.utc) if item.get("EditTime") else None
                url = canonicalize(item.get("Url", "")) if item.get("Url") else ""
                aliases = {str(x).strip().lower() for x in channel.get("author_aliases", [source["name"]])}
                author = str(item.get("AuthorName") or "").strip().lower()
                if (not url or url in seen or not published or published.date() < cutoff_date or not item.get("Title")
                        or not any(alias and (author == alias or alias in author) for alias in aliases)):
                    continue
                seen.add(url)
                records.append(Record(
                    id=stable_id("media", url), category="media", title=item["Title"], url=url,
                    source_name=source["name"], source_id=source["id"], channel_id=channel["id"],
                    published_at=published, collected_at=datetime.now(timezone.utc),
                    summary=(item.get("ContentText") or "")[:1600], priority=channel.get("priority", source.get("priority", "P1")),
                    source_urls=[url], metadata={"author": item.get("AuthorName"), "author_badge": item.get("AuthorBadgeText"),
                    "authority": item.get("AuthorityLevel"), "vote_up": item.get("VoteUpCount", 0),
                    "content_type": item.get("ContentType"), "claim_status": "single_source_reporting"},
                ))
            records.sort(key=lambda r: (int(r.metadata.get("authority") or 0), r.metadata.get("vote_up", 0)), reverse=True)
            return records, RunReport(source_id=source["id"], channel_id=channel["id"], status="success", fetched=len(items), selected=len(records))
        except Exception as exc:
            return [], RunReport(source_id=source["id"], channel_id=channel["id"], status="fetch_error", error=str(exc)[:500])

    def wechat_sogou(self, source: dict, channel: dict) -> tuple[list[Record], RunReport]:
        try:
            response = self.get_with_backoff("https://weixin.sogou.com/weixin", params={"type": "2", "query": channel.get("query", source["name"]), "ie": "utf8"},
                                             headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
            if response.status_code == 429:
                return [], RunReport(source_id=source["id"], channel_id=channel["id"], status="rate_limited",
                                     error="Sogou WeChat rate limit after retries")
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
            records = []
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=7)).date()
            for node in soup.select("ul.news-list > li"):
                anchor = node.select_one("h3 a[href]")
                account_node = node.select_one("div.s-p .all-time-y2") or node.select_one("div.s-p a")
                if not anchor:
                    continue
                account = account_node.get_text(" ", strip=True) if account_node else ""
                aliases = [str(x).strip().lower() for x in channel.get("account_aliases", [source["name"]])]
                if not any(alias and alias in account.strip().lower() for alias in aliases):
                    continue
                timestamp = None
                match = re.search(r"timeConvert\(['\"]?(\d+)", str(node))
                if match:
                    timestamp = datetime.fromtimestamp(int(match.group(1)), timezone.utc)
                if not timestamp or timestamp.date() < cutoff_date:
                    continue
                url = canonicalize(urljoin("https://weixin.sogou.com", anchor.get("href", "")))
                summary_node = node.select_one("p.txt-info")
                records.append(Record(
                    id=stable_id("media", url), category="media", title=anchor.get_text(" ", strip=True), url=url,
                    source_name=source["name"], source_id=source["id"], channel_id=channel["id"],
                    published_at=timestamp, collected_at=datetime.now(timezone.utc),
                    summary=summary_node.get_text(" ", strip=True)[:1200] if summary_node else "",
                    priority=channel.get("priority", "P1"), source_urls=[url],
                    metadata={"account": account, "discovery_source": "sogou_wechat", "claim_status": "single_source_reporting"},
                ))
            return records, RunReport(source_id=source["id"], channel_id=channel["id"], status="success", fetched=len(soup.select("ul.news-list > li")), selected=len(records))
        except Exception as exc:
            return [], RunReport(source_id=source["id"], channel_id=channel["id"], status="fetch_error", error=str(exc)[:500])


def deduplicate(records: list[Record]) -> list[Record]:
    by_key: dict[str, Record] = {}
    for record in records:
        title_key = re.sub(r"\W+", "", record.title.lower())
        key = record.id if record.id.startswith("arxiv:") else record.url or title_key
        if key in by_key:
            current = by_key[key]
            current.source_urls = sorted(set(current.source_urls + record.source_urls))
            current.metadata.setdefault("source_memberships", []).append({"source_id": record.source_id, "channel_id": record.channel_id})
        else:
            by_key[key] = record
    return list(by_key.values())


def collect_paperlab(database: Path, source: dict, lookback_days: int = 365) -> tuple[list[Record], RunReport]:
    """Read papers published within the lookback window from PaperLab, read-only."""
    try:
        if not database.is_file():
            raise FileNotFoundError(database)
        venues = {
            str(c.get("paperlab_venue") or c["id"]).upper()
            for c in source.get("conferences", [])
            if c.get("enabled", True) and c.get("mode") == "paperlab"
        }
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()
        placeholders = ",".join("?" for _ in venues)
        query = f"""
            SELECT p.id, p.arxiv_id, p.doi, p.title, p.abstract, p.published_date,
                   p.source_url, p.pdf_url, v.venue, v.year, v.track, v.first_seen,
                   p.relevance_score, p.keywords
            FROM papers p JOIN paper_venues v ON v.paper_id = p.id
            WHERE upper(v.venue) IN ({placeholders}) AND p.deleted_at IS NULL
              AND (p.published_date >= ? OR v.year >= ?)
            ORDER BY coalesce(p.relevance_score, 0) DESC, v.first_seen DESC
            LIMIT 600
        """
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        rows = db.execute(query, (*sorted(venues), cutoff_date.isoformat(), cutoff_date.year)).fetchall()
        db.close()
        records = []
        for row in rows:
            paper_id, arxiv_id, doi, title, abstract, published, source_url, pdf_url, venue, year, track, first_seen, relevance, keywords = row
            url = source_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else pdf_url)
            if not url:
                continue
            published_at = parse_time(published)
            if not published_at:
                published_at = datetime(int(year), 1, 1, tzinfo=timezone.utc)
            if published_at.date() < cutoff_date:
                continue
            records.append(Record(
                id=f"arxiv:{arxiv_id}" if arxiv_id else f"paperlab:{paper_id}",
                category="paper", title=title, url=canonicalize(url), source_name=source["name"],
                source_id=source["id"], channel_id=f"{str(venue).lower()}-{year}",
                published_at=published_at,
                collected_at=datetime.now(timezone.utc), summary=(abstract or "")[:2000],
                priority=next((c.get("priority", "P1") for c in source.get("conferences", [])
                               if str(c.get("paperlab_venue") or c["id"]).upper() == str(venue).upper()), "P1"),
                source_urls=[canonicalize(x) for x in (source_url, pdf_url) if x],
                metadata={"venue": venue, "year": year, "track": track, "doi": doi,
                          "relevance_score": relevance or 0, "keywords": keywords or "",
                          "discovered_at": datetime.fromtimestamp(first_seen, timezone.utc).isoformat()},
            ))
        return records, RunReport(source_id=source["id"], channel_id="conference-catalog", status="success", fetched=len(rows), selected=len(records))
    except Exception as exc:
        return [], RunReport(source_id=source["id"], channel_id="conference-catalog", status="fetch_error", error=str(exc)[:500])


def collect_all(settings) -> tuple[list[Record], list[RunReport]]:
    collector = Collector(settings.runtime.get("request_timeout_seconds", 30))
    records, reports, jobs = [], [], []
    company_defaults = settings.corporations.get("defaults", {})
    for corp in settings.corporations.get("corporations", []):
        if not corp.get("enabled", company_defaults.get("enabled", False)):
            continue
        for channel in corp.get("channels", []):
            if channel.get("enabled", company_defaults.get("channel_enabled", True)) is False:
                continue
            kind = channel.get("type")
            if kind in {"rss", "github_release"}:
                jobs.append((corp["id"], channel["id"], collector.feed, ("company", corp["id"], corp["name"], channel)))
            elif kind == "sitemap":
                jobs.append((corp["id"], channel["id"], collector.sitemap, ("company", corp["id"], corp["name"], channel)))
            elif kind == "html_monitor":
                jobs.append((corp["id"], channel["id"], collector.html_listing, ("company", corp["id"], corp["name"], channel)))
            elif kind == "github_org":
                jobs.append((corp["id"], channel["id"], collector.github_org, ("company", corp["id"], corp["name"], channel)))
            elif kind == "huggingface_org":
                jobs.append((corp["id"], channel["id"], collector.huggingface_org, ("company", corp["id"], corp["name"], channel)))
            elif kind == "api" and "daily_papers" in channel.get("endpoint", ""):
                reports.append(RunReport(source_id=corp["id"], channel_id=channel["id"], status="delegated_to_papers"))
            else:
                reports.append(RunReport(source_id=corp["id"], channel_id=channel["id"], status="unsupported", error=f"adapter {kind} requires a curated/session integration"))

    media_defaults = settings.media.get("defaults", {})
    for source in settings.media.get("sources", []):
        if not source.get("enabled", media_defaults.get("enabled", False)):
            continue
        for channel in source.get("channels", []):
            if channel.get("enabled", media_defaults.get("channel_enabled", True)) is False:
                continue
            adapter = channel.get("adapter")
            if adapter in {"rss", "atom"}:
                jobs.append((source["id"], channel["id"], collector.feed, ("media", source["id"], source["name"], channel)))
            elif adapter == "html_listing":
                jobs.append((source["id"], channel["id"], collector.html_listing, ("media", source["id"], source["name"], channel)))
            elif adapter == "gzip_sitemap":
                jobs.append((source["id"], channel["id"], collector.gzip_sitemap, (source, channel)))
            elif adapter == "zhihu_search_mcp":
                jobs.append((source["id"], channel["id"], collector.zhihu_search, (source, channel)))
            elif adapter == "wechat_sogou_search":
                jobs.append((source["id"], channel["id"], collector.wechat_sogou, (source, channel)))
            else:
                reports.append(RunReport(source_id=source["id"], channel_id=channel["id"], status="unsupported", error=f"adapter {adapter} requires MCP/search session"))

    for source in settings.papers.get("sources", []):
        if not source.get("enabled", settings.papers.get("defaults", {}).get("enabled", True)):
            continue
        if source.get("adapter") == "huggingface_daily_papers":
            jobs.append((source["id"], "daily-papers", collector.hf_papers, (source,)))
        elif source.get("adapter") == "paperlab":
            database = Path(settings.runtime.get("paperlab", {}).get("database", source.get("database", "")))
            jobs.append((source["id"], "conference-catalog", collect_paperlab, (database, source, 365)))

    with ThreadPoolExecutor(max_workers=int(settings.runtime.get("collection_workers", 8))) as pool:
        futures = {pool.submit(function, *args): (source_id, channel_id, datetime.now(timezone.utc))
                   for source_id, channel_id, function, args in jobs}
        for future in as_completed(futures):
            source_id, channel_id, started_at = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = ([], RunReport(source_id=source_id, channel_id=channel_id, status="fetch_error", error=str(exc)[:500]))
            source = settings.source(source_id) or {}
            result[1].started_at = result[1].started_at or started_at
            result[1].finished_at = result[1].finished_at or datetime.now(timezone.utc)
            if result[1].status == "success" and not result[1].parsed and result[1].selected:
                result[1].parsed = result[1].selected
            for record in result[0]:
                record.language = source.get("language", record.language)
                record.source_priority = PRIORITY_VALUES.get(source.get("priority", record.priority), record.source_priority)
                record.discovered_at = record.discovered_at or record.collected_at
                record.metadata.setdefault("source_priority", record.source_priority)
                record.metadata.setdefault("language", record.language)
                record.metadata.setdefault("parse_status", record.parse_status)
            records.extend(result[0]); reports.append(result[1])
    completed_at = datetime.now(timezone.utc)
    for report in reports:
        report.started_at = report.started_at or completed_at
        report.finished_at = report.finished_at or completed_at
    return deduplicate(records), reports


def collect_source(settings, source_id: str) -> tuple[list[Record], list[RunReport]]:
    """Run only one configured source, including all of its enabled channels."""
    if settings.source(source_id) is None:
        raise ValueError(f"unknown source: {source_id}")
    for section, key in ((settings.corporations, "corporations"),
                         (settings.media, "sources"), (settings.papers, "sources")):
        for source in section.get(key, []):
            source["enabled"] = source.get("id") == source_id
    return collect_all(settings)
