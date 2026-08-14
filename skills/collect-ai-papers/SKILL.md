---
name: collect-ai-papers
description: Collect, audit, normalize, and deduplicate top-conference papers from the local PaperLab catalogue and high-attention new arXiv papers from Hugging Face Daily Papers. Use when Clippers needs conference-paper updates for KDD, ICML, NeurIPS, MLSys, ICLR, OSDI, SOSP, NSDI, FAST, SIGCOMM, ASPLOS, ISCA, MICRO, HPCA, USENIX ATC, or EuroSys; trending-paper discovery; PaperLab completeness checks; or paper inputs for a digest.
---

# Collect AI Papers

Use `config/papers.yaml` as the source of truth. Treat PaperLab as the primary conference catalogue and Hugging Face Daily Papers as a popularity-based discovery layer for arXiv.

## Workflow

1. Validate configuration:

   ```bash
   ruby skills/collect-ai-papers/scripts/validate_papers.rb config/papers.yaml
   ```

2. For PaperLab, run incremental update before querying when the configured schedule is due. Do not run full refresh unless explicitly requested.
3. Read enabled conferences from PaperLab by canonical venue ID. Missing conference rows are errors unless the venue/year is recorded as `pending` or `announced`.
4. Handle USENIX ATC as an archive source. Handle EuroSys through DBLP and the official program until PaperLab supports it.
5. Fetch Hugging Face Daily Papers, apply lookback and popularity rules, then apply the configured topic filter.
6. Enrich Hugging Face results from arXiv by arXiv ID. Do not treat Hugging Face metadata as a second paper identity.
7. Normalize, deduplicate, and persist source memberships before sending papers to summarization.

Read [references/collection-methods.md](references/collection-methods.md) before auditing conference completeness, changing source precedence, or implementing the collectors.

## Conference rules

- Preserve title, authors, organizations, abstract, awards, track, source URL, venue, and year when available.
- Compare normalized title sets; equal counts do not prove equality.
- Report `matched`, `missing`, `stale`, and `parse_error` separately.
- Never delete stale records automatically.
- Preserve multiple venue/year memberships for a paper.
- Treat upcoming pages without accepted lists as `announced` or `pending`, not empty conferences.

## Hugging Face rules

- Use `paper.id` as the arXiv ID and deduplication key.
- Rank within the configured lookback window using upvotes first and comments second.
- Keep `submittedOnDailyAt` distinct from the arXiv publication timestamp.
- Treat popularity as a discovery signal, not a quality score.
- Record papers excluded by topic filtering so thresholds can be audited.

## Output

Return normalized paper records plus per-source counts for fetched, selected, filtered, duplicate, missing, stale, pending, and failed items. Include the exact source URL and collection status for every record.

