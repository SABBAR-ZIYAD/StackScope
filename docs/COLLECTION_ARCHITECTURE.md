# Collection architecture

## Audit of the previous collector

The previous implementation correctly kept REST Search for discovery, split star ranges around
GitHub's 1,000-result ceiling, deduplicated repositories, checkpointed completed slices, and reused
the repository metadata returned by Search.

Its enrichment query was inefficient at scale. GraphQL fetched root entries, workflow entries and
five individual manifest paths for every repository. It could not discover nested manifests, had no
commit-pinned file cache, did not handle truncated trees, and treated GraphQL as the only enrichment
route.

## Current adaptive design

1. REST Search discovers repositories. Query slices, pages and checkpoints remain in DuckDB.
2. The collector reuses Search metadata instead of calling `GET /repos/{owner}/{repo}` again.
3. `auto` enrichment uses one GraphQL batch for default-branch commit/tree SHAs and language byte
   counts when a token and multiple repositories are available. `optimized_rest` uses commit and
   language endpoints. A failed or partial GraphQL batch falls back per repository to REST.
4. The recursive Git Trees endpoint discovers dependency manifests and configuration evidence in
   one request. A bounded breadth-first walk handles truncated recursive trees.
5. Relevant public files are fetched from `raw.githubusercontent.com` at the immutable commit SHA,
   with the Git Blob API as a fallback. File size limits are applied before parsing.
6. Tree and file results are cached in DuckDB by repository ID, commit SHA, tree SHA, path and blob
   SHA. Completed repository enrichment rows are checkpoints, so interrupted runs resume pending
   repositories only. `collect enrich --refresh` performs an incremental commit check and reuses
   cached trees and blobs when their identities have not changed.
7. A shared manager tracks REST core, REST search and GraphQL budgets separately. Collection is
   deliberately sequential, keeps configurable quota reserves, rotates tokens, obeys
   `Retry-After`, and applies a shared secondary-limit cooldown.

## Archive decision

Archive retrieval is supported with download, repository-size, entry-count, extracted-byte and
per-file limits. Symlinks, hard links, devices and traversal paths are never extracted.

The 2026-11-22 three-repository benchmark showed that automatic archives transferred about 14.2 MB
and made both optimized strategies slower, while the old root-file checks transferred only 45 KB.
Consequently `archive_mode = "never"` is the safe default. Use `auto` only for a workload where a
representative benchmark shows that many relevant files justify the bandwidth.

## Strategy selection

- `auto`: hybrid for authenticated batches of two or more repositories; optimized REST otherwise.
- `hybrid`: batched GraphQL snapshots plus REST Trees and commit-pinned file retrieval.
- `optimized_rest`: REST commit/language snapshots plus the same Trees and file retrieval.

GraphQL is an optimization, not a dependency. With the production selective-retrieval policy, the
live benchmark used one GraphQL point, reduced core REST requests from 9 to 3, transferred slightly
fewer bytes and completed faster than optimized REST for three repositories. Hybrid is therefore
selected for authenticated multi-repository batches; optimized REST remains the appropriate fallback
when GraphQL is unavailable or less efficient for the current workload.

## Benchmark interpretation

`gem benchmark-collection` executes each strategy against the same repository identities with an
isolated cache and records requests, GraphQL points, bytes, duration, rate-limit errors and completed
repositories in `collection_benchmarks`. REST Search is identical for all strategies and is therefore
outside the enrichment comparison.

The legacy strategy is not capability-equivalent: it checks only configured root paths and can miss
nested manifests and configuration files. Request and byte numbers must be interpreted alongside
coverage, not alone.
