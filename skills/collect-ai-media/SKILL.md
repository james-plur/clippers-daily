---
name: collect-ai-media
description: Collect, filter, verify, normalize, and deduplicate Chinese and English AI media reporting configured in Clippers. Use when collecting 机器之心、新智元、Reuters or other enabled media sources, producing media candidates for an AI digest, separating reporting from commentary, or reconciling media stories with company blogs and papers.
---

# Collect AI Media

Use `config/media.yaml` as the source of truth. Keep media reporting separate from first-party company updates in `config/coporations.yaml` and papers in `config/papers.yaml`.

## Enablement

- Skip a source unless `source.enabled: true`.
- For an enabled source, enable every channel unless that channel explicitly has `enabled: false`.
- Never infer enablement from priority.
- Keep disabled sources visible as review candidates but do not collect them.

## Workflow

1. Validate the configuration:

   ```bash
   ruby skills/collect-ai-media/scripts/validate_media.rb config/media.yaml
   ```

2. Resolve enabled sources and inherited channel enablement.
3. Fetch listing metadata within the configured lookback window. Prefer a stable API or RSS when one is verified; otherwise use domain-restricted search, HTML listing, MCP, or a browser as configured.
4. Apply the AI Infra topic filter before fetching full articles.
5. Resolve each story to its canonical URL, publication time, author, and content type.
6. Find any cited primary source. Merge media and primary-source memberships instead of emitting duplicate stories.
7. Cache only permitted metadata, excerpts, and derived summaries; respect paywalls, robots rules, and publisher restrictions.
8. Return selected items with collection status and evidence URLs.

Read [references/collection-methods.md](references/collection-methods.md) before implementing adapters, changing source precedence, or troubleshooting a blocked source.

## Editorial rules

- Label `news`, `analysis`, `opinion`, and `rumor` separately.
- Prefer a primary source for the factual core, while retaining genuinely useful media investigation or analysis.
- Do not convert a media claim into a confirmed fact without corroboration.
- Attribute exclusive reporting and analysis to the publisher.
- Treat Reuters Breakingviews as analysis, not straight news.
- Do not let the same announcement appear once from a company blog and again as an unrelated media item.
- Preserve Chinese and English titles when available; generate a translated title as a separate field.

## Output

Return normalized records plus per-source counts for `fetched`, `selected`, `filtered`, `duplicate`, `blocked`, `rate_limited`, and `failed`. Include canonical URL, source memberships, publication time, content type, primary-source URL when found, and the reason each item was selected.
