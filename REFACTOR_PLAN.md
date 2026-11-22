# StackScope Collection Refactor Plan

## What already works

- REST Search discovers repositories with star-range partitioning, pagination,
  deduplication, run limits, and DuckDB checkpoints.
- Search responses are already persisted as repository metadata.
- GraphQL batches repository enrichment and reports point consumption.
- The HTTP client distinguishes primary limits, secondary limits, permission
  failures, server failures, and retryable network errors.
- Runs are isolated by configuration fingerprint and are resumable.
- Parsing, normalization, evidence creation, association mining, tests, and CI
  are functional and must remain compatible.

## What can be reused

- `collect_search()` and the repository table remain the discovery layer.
- `GitHubClient` remains the single authenticated transport.
- Run manifests, stage tracking, DuckDB access, and API-call logging remain the
  operational foundation.
- Existing manifest parsers continue to consume a `path -> text/blob metadata`
  mapping.

## What must be modified

- GraphQL enrichment currently requests root trees and file contents directly.
  It will be narrowed to batched commit/tree identity and language statistics.
- API accounting will track REST core, REST search, and GraphQL separately,
  including response bytes and secondary-limit cooldowns.
- Enrichment will be checkpointed per immutable repository commit.
- The schema will gain explicit versioned migrations rather than rejecting
  every older database.
- Manifest parsing will accept relevant files below the repository root.

## What must be added

- Recursive Git Trees discovery with a bounded non-recursive fallback for
  truncated trees.
- Selective immutable file retrieval using commit-SHA raw URLs, with Git Blob
  fallback.
- An optional, guarded archive path for repositories with many relevant files.
- Persistent tree and file/blob caches keyed by repository ID, commit SHA,
  path, and blob SHA.
- Adaptive strategy selection and collection metrics.
- A benchmark comparing naive REST, optimized REST, and hybrid REST + GraphQL.
- Tests for pagination, budgets, retries, truncated trees, cache hits,
  interrupted enrichment, archive safety, and GraphQL partial failures.

## Implementation phases

1. Add configuration, rate-budget state, schema migration, and cache tables.
2. Narrow GraphQL enrichment and implement Git Trees discovery.
3. Add immutable selective retrieval and guarded archive extraction.
4. Replace enrichment orchestration while preserving its stored output shape.
5. Add the three-strategy benchmark and collection documentation.
6. Run the complete test suite and a small public-repository benchmark when a
   usable token is available.

The measured benchmark, not a predetermined preference for GraphQL, will guide
the default enrichment strategy.
