# Media collection methods

## Contents

- Source boundaries
- Adapter precedence
- Source-specific guidance
- Normalized schema
- Deduplication and verification
- Failure handling

## Source boundaries

Media sources provide independent reporting, synthesis, interviews, investigations, and opinion. Company announcements belong to `config/coporations.yaml`; academic metadata belongs to `config/papers.yaml`. A media article that merely rewrites a press release should normally enrich the primary item rather than create a second digest entry.

## Adapter precedence

Use adapters in this order when available and verified: publisher API, valid RSS/Atom, HTML listing, domain-restricted search, MCP discovery, rendered browser. Do not label an HTML response as RSS based only on its path. Record the actual adapter and fetch status on every run.

## Source-specific guidance

- **机器之心:** collect the official site first. Use Zhihu search as discovery and redundancy. Canonicalize to the publisher URL when possible.
- **新智元:** use Zhihu MCP as the current primary discovery route. Treat WeChat as a gap-filling channel until an adapter is validated. Do not invent or assume an official standalone domain.
- **Reuters:** locate AI and Breakingviews items by domain-restricted search when direct pages block automated access. Do not bypass authentication or access controls. Distinguish Reuters reporting from Breakingviews commentary.
- **Other English candidates:** collect only after their source-level switch is enabled. Expect subscriptions, rate limits, or HTML layout changes.

## Normalized schema

```yaml
id: url-sha256:...
title: "..."
translated_title: null
language: en
published_at: "2026-08-04T00:00:00Z"
authors: []
content_type: news
canonical_url: https://example.com/article
primary_source_url: null
topics: []
source_memberships:
  - source: reuters-ai
    channel: ai-topic
selection_reason: "AI infrastructure financing"
collection_status: selected
```

## Deduplication and verification

Canonicalize tracking parameters and redirects, then use canonical URL, normalized headline similarity, and shared primary-source URL. Keep multiple media memberships on one event record. Prefer the earliest authoritative timestamp but retain each publisher timestamp.

Classify claims as `confirmed_primary`, `confirmed_multiple_media`, `single_source_reporting`, `analysis`, or `rumor`. A single source may still be valuable, but its status must remain visible.

## Failure handling

Distinguish `not_modified`, `empty`, `blocked`, `authentication_required`, `rate_limited`, `fetch_error`, and `parse_error`. Preserve the last successful snapshot. Never interpret 401, 403, 429, an interstitial, or an empty HTML shell as “no new articles.”
