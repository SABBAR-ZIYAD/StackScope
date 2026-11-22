# Collection benchmark results

Date: 2026-11-22  
Sample: `donnemartin/system-design-primer`, `louislam/uptime-kuma`, `imputnet/cobalt`  
Configuration: selective commit-pinned file retrieval (`archive_mode = "never"`)

| Strategy | Analyzed | Core REST | GraphQL requests / points | Bytes | Duration | Limit errors |
|---|---:|---:|---:|---:|---:|---:|
| Existing REST-only | 3/3 | 24 | 0 / 0 | 45,445 | 14.507 s | 0 |
| Optimized REST-only | 3/3 | 9 | 0 / 0 | 551,435 | 38.513 s | 0 |
| Hybrid REST + GraphQL | 3/3 | 3 | 1 / 1 | 527,559 | 26.572 s | 0 |

REST Search is common to all approaches and was excluded from this enrichment benchmark.

The existing collector appears cheapest in bytes and time because it only probes five configured
root paths. It does not provide equivalent coverage: it misses nested manifests and broader
configuration evidence. Between the two capability-equivalent implementations, hybrid used six
fewer core requests, transferred 23,876 fewer bytes and completed about 31% faster in this run.
Therefore `auto` selects hybrid for authenticated multi-repository batches, with per-repository REST
fallback.

An earlier archive-enabled trial transferred approximately 14.2 MB and took 32.734 seconds for
optimized REST and 39.784 seconds for hybrid. That measurement is why archives remain available but
are disabled by default.

This is a deliberately small smoke benchmark, not a universal performance claim. Re-run
`gem benchmark-collection` on a representative sample before changing the strategy or archive
policy for a large study.
