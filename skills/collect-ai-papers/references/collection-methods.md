# Paper collection methods

## Contents

- Source roles
- Conference collection
- Hugging Face trending collection
- Normalized paper schema
- Deduplication
- Failure handling

## Source roles

- PaperLab is the source of truth for supported conference catalogues.
- Official accepted/program/proceedings pages audit PaperLab completeness.
- DBLP is the stable fallback after proceedings publication and the temporary primary source for EuroSys.
- Hugging Face Daily Papers discovers community-attention arXiv papers; arXiv remains the paper identity and metadata authority.

## Conference collection

Query `/Users/luchenda/tools/paperlab/data/papers.db` read-only for ordinary collection. Use the PaperLab CLI `update` command for scheduled incremental synchronization and `search` only for semantic retrieval.

For each venue/year, retain a sync state: `published`, `announced`, `pending`, or `fetch_error`. Validate normalized title sets rather than counts alone.

Provider guidance:

| Venue | Primary catalogue | Audit/fallback |
|---|---|---|
| KDD, ICML, NeurIPS, MLSys, ICLR | PaperLab | official proceedings, DBLP/OpenReview as applicable |
| OSDI, NSDI, FAST | PaperLab | USENIX technical sessions |
| SOSP, SIGCOMM, ASPLOS, ISCA, MICRO | PaperLab | DBLP; official/Wayback/browser for live accepted lists |
| HPCA | PaperLab | researchr and DBLP |
| USENIX ATC | external archive | DBLP and USENIX archive |
| EuroSys | DBLP until imported | official EuroSys program |

USENIX pages must be fetched without `--compressed` or browser-style `Sec-Fetch-*` headers because those can produce HTTP 200 with an empty body. ACM-hosted pages may require DBLP, Wayback, or a rendered browser.

## Hugging Face trending collection

Fetch `https://huggingface.co/api/daily_papers?limit=N`. Preserve:

- `paper.id` as `arxiv_id`;
- `paper.title`, `paper.authors`, and `paper.summary`;
- `paper.publishedAt` and `paper.submittedOnDailyAt` separately;
- `paper.upvotes`, top-level `numComments`, and `paper.discussionId`.

Filter by the configured lookback using `submittedOnDailyAt`, rank by upvotes and comments, apply `daily_top_n`, then apply the topic filter. Fetch canonical arXiv metadata by ID after selection.

## Normalized paper schema

```yaml
id: arxiv:2607.29377
title: "..."
authors: []
organizations: []
abstract: "..."
arxiv_id: "2607.29377"
doi: null
venues: []
tracks: []
published_at: "2026-07-31T00:00:00Z"
discovered_at: "2026-08-04T00:00:00Z"
topics: []
source_memberships:
  - source: huggingface-daily-papers
    upvotes: 1
    comments: 0
    discussion_id: null
source_urls: []
collection_status: selected
```

## Deduplication

Use identities in this order: arXiv ID, DOI, normalized title hash. Merge source memberships instead of creating a new paper row. Preserve distinct venue/year memberships and popularity snapshots.

## Failure handling

Distinguish `not_modified`, `pending`, `announced`, `fetch_error`, `parse_error`, `rate_limited`, and `blocked`. An empty response is a fetch or parse error unless the authoritative source explicitly represents an empty result. Never overwrite the last successful conference snapshot after a failure.

