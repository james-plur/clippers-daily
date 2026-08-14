# Company update collection methods

## Contents

- Configuration contract
- Fetch strategies
- Normalized record
- Deduplication and precedence
- Filtering and scoring
- Failure handling

## Configuration contract

`config/coporations.yaml` uses corporation-level opt-in and channel-level opt-out. When a corporation has `enabled: true`, all of its channels are active by default. A channel is inactive only when it explicitly has `enabled: false`. `priority` is user-editable scheduling and presentation guidance, not an enablement signal.

Required corporation fields: `id`, `name`, `enabled`, `priority`, `focus`, `channels`.

Required channel fields: `id`, `name`, `priority`, `type`, `endpoint`, `schedule`. The optional channel `enabled` field defaults to `true`.

Use stable lowercase kebab-case IDs. Never change an existing ID merely because the display name changes.

## Fetch strategies

### RSS and Atom

Use conditional GET, follow redirects, and store ETag/Last-Modified. Use GUID when stable; otherwise use canonical URL. Parse both `published` and `updated`. Retain categories for deterministic topic filtering.

### Sitemap

Store the previous URL-to-lastmod mapping. Apply `url_filter` before scheduling page fetches. A missing `lastmod` requires a content hash. Do not assume sitemap order is chronological.

### GitHub

Prefer Release Atom for public repositories and authenticated Releases API when pagination or assets matter. Use `repo + tag` as the identity. For organization sources, maintain a repository allowlist; organization-wide commits and issues are too noisy by default.

### Product changelog and HTML

Extract index entries into title, URL, date, and summary. Normalize navigation and tracking parameters before hashing. Use a rendered browser only for access blocks or client-rendered lists. Store a selector/extraction version so parser changes can be audited.

### Model hubs

Track model ID, revision/commit, updated time, model card, license, tags, and related paper. Model-card-only changes should be marked separately from new model weights.

## Normalized record

```yaml
id: stable-content-id
corporation_id: nvidia
channel_id: technical-blog
content_type: technical_blog
title: "..."
url: "https://..."
canonical_url: "https://..."
published_at: "2026-08-02T00:00:00Z"
updated_at: null
version: null
repository: null
tags: [inference, gpu]
summary: "..."
raw_hash: sha256:...
collected_at: "2026-08-02T06:00:00Z"
source_priority: P0
shadow: true
```

## Deduplication and precedence

Use these keys in order:

1. repository and release tag;
2. DOI, arXiv ID, or model ID and revision;
3. canonical URL without tracking parameters;
4. corporation, normalized title, and a seven-day time window.

When one announcement appears in several channels, preserve all source links but choose the primary record in this order:

1. product release notes or GitHub Release for exact version facts;
2. engineering or research article for technical explanation;
3. corporate news for context;
4. social-media post only as a lead.

## Filtering and scoring

Apply deterministic focus-tag and keyword filtering before LLM classification. Score retained results on technical depth, AI-infrastructure relevance, novelty, and source authority. Do not discard a product release solely because its title lacks keywords; use repository/channel focus as context.

## Failure handling

Persist one of: `success`, `not_modified`, `auth_error`, `rate_limited`, `blocked`, `fetch_error`, `parse_error`, `invalid_timestamp`.

Retry transient failures with bounded exponential backoff. A parser returning zero items after previously returning items is a parse error until inspected. Never overwrite the last successful cursor or snapshot after a failed run.
