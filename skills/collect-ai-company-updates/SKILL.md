---
name: collect-ai-company-updates
description: Collect, verify, normalize, and deduplicate official technology updates from AI companies configured in Clippers coporations.yaml. Use when Clippers needs to poll company research or engineering blogs, product changelogs, release notes, sitemaps, GitHub releases, model hubs, or official news; audit whether configured sources still work; or produce company technology-update inputs for a digest.
---

# Collect AI Company Updates

Use `config/coporations.yaml` as the source of truth. Never enable a corporation based on priority alone.

## Workflow

1. Validate the configuration:

   ```bash
   ruby skills/collect-ai-company-updates/scripts/validate_corporations.rb config/coporations.yaml
   ```

2. Select every channel under a corporation with `enabled: true`. Treat a missing channel `enabled` field as `true`; skip only channels explicitly set to `enabled: false`.
3. Fetch with the adapter named by `type`. Prefer conditional requests using `ETag` and `Last-Modified`.
4. Preserve the raw response and record fetch status before extracting content.
5. Normalize every result to the schema in [references/collection-methods.md](references/collection-methods.md).
6. Deduplicate related blog, product, model, and repository announcements before summarization.
7. Run new or changed sources in shadow mode for `defaults.shadow_days`; do not place shadow results in the production digest.

## Adapter selection

- `rss`: parse RSS/Atom and retain GUID, canonical URL, published/updated timestamps, title, summary, and categories.
- `sitemap`: diff URL and `lastmod` sets, apply `url_filter`, then fetch only new or changed pages.
- `github_release`: read Atom or GitHub Releases API; key records by repository and tag.
- `github_org`: enumerate a curated repository allowlist before polling releases. Do not ingest all organization commits.
- `html_monitor`: extract the article or changelog index, compare normalized links and content hashes, and fetch changed detail pages.
- `browser_monitor`: use only when ordinary HTTP access is blocked or the page requires rendered JavaScript.
- `huggingface_org`: poll model metadata and model-card changes through the Hugging Face API.
- `api`: use the documented official endpoint and persist pagination/cursor state.

Read [references/collection-methods.md](references/collection-methods.md) before implementing an adapter, changing source precedence, or diagnosing missing/duplicate updates.

## Reliability rules

- Treat an empty response as `fetch_error` until a valid empty payload is proven.
- Do not replace an official source with social-media search. Social posts may supply leads only.
- Do not infer publication time from collection time when the source provides no timestamp; mark it unknown.
- Record `auth_error`, `rate_limited`, `blocked`, `parse_error`, and `not_modified` separately.
- Keep browser monitoring as the last fallback because it is costly and fragile.
- Never send raw HTML, duplicate announcements, or unverified timestamps directly to the daily digest.

## Output

Return normalized records plus a per-channel run report containing fetched, new, changed, duplicate, filtered, and failed counts. Include the source URL for every record and the exact failure reason for every failed channel.
