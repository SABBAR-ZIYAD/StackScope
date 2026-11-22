# GitHub Ecosystem Miner Improvement Roadmap

This document is the implementation plan for turning GitHub Ecosystem Miner
from a language-only baseline into a reusable, scalable, reproducible, and
well-tested platform for studying open-source ecosystems.

The work is intentionally ordered. Complete and verify one step before moving
to the next. The existing pipeline should remain usable while the early
refactoring steps are in progress.

## Product direction

GitHub Ecosystem Miner should become an engine that can run many ecosystem
studies, not a one-time analysis tied to one query or programming language.

The finished project should be able to:

- collect thousands of repositories without losing progress;
- keep different collection runs and configurations isolated;
- identify packages, tools, and meaningful languages with traceable evidence;
- clean data without silently discarding or changing records;
- mine reliable association rules and technology communities;
- discover stable, explainable repository families;
- reproduce an analysis without making new GitHub API requests;
- generate a complete report from a recorded run; and
- verify the critical behavior through automated tests.

## Principles to preserve

The current project already has useful design choices that must not be lost:

- raw data is separated from processed and mined data;
- collection can resume from existing files;
- files are written atomically;
- collection configuration is checked to prevent accidental dataset mixing;
- stages can be rerun independently; and
- the pipeline is understandable without a large framework.

## Roadmap status

- [x] Step 0 — Establish and protect the baseline
- [x] Step 1 — Refactor scripts into an import-safe application
- [x] Step 2 — Introduce validated configuration and run manifests
- [x] Step 3 — Add a database-backed collection model
- [x] Step 4 — Build a production-grade GitHub API client
- [x] Step 5 — Scale repository discovery beyond 1,000 results
- [x] Step 6 — Collect rich technology evidence efficiently
- [x] Step 7 — Build an auditable cleaning and normalization layer
- [x] Step 8 — Upgrade association-rule and stack mining
- [x] Step 9 — Upgrade clustering and cluster interpretation
- [x] Step 10 — Add analytical robustness and temporal analysis
- [x] Step 11 — Expand tests, schemas, and continuous integration
- [x] Step 12 — Generate reports and an optional explorer
- [x] Step 13 — Optimize, package, and prepare the project for release

---

## Step 0 — Establish and protect the baseline

### Goal

Record the current behavior before changing the architecture. This gives us a
reference for detecting accidental changes during the refactor.

### Work

1. Run and record the existing unit tests.
2. Create a small, committed fixture dataset that does not contain secrets and
   does not require network access.
3. Run the existing processing stages on that fixture and record:
   - repository count;
   - language count;
   - transformed matrix shape;
   - association-rule count; and
   - selected cluster count.
4. Add a small end-to-end smoke test using the fixture.
5. Document the current input and output schema of every stage.
6. Record known limitations and current bugs instead of silently fixing them
   during the structural refactor.

### Deliverables

- `tests/fixtures/` with a small deterministic dataset;
- baseline expectations used by tests; and
- a short schema description for the current artifacts.

### Complete when

- the existing four tests pass;
- the fixture pipeline runs without a GitHub token; and
- later refactoring can be compared against deterministic baseline outputs.

---

## Step 1 — Refactor scripts into an import-safe application

### Goal

Remove module-level side effects and provide one clear command-line interface
without changing the pipeline's results.

### Work

1. Move executable code in each script into small functions.
2. Add `main()` functions and `if __name__ == "__main__"` guards.
3. Separate input/output code from transformation logic so transformations can
   be unit-tested with in-memory data.
4. Introduce a package-oriented structure such as:

   ```text
   src/github_ecosystem_miner/
   ├── cli.py
   ├── config.py
   ├── collection/
   ├── processing/
   ├── mining/
   └── reporting/
   ```

5. Add a single CLI as the supported application interface.
6. Provide commands similar to:

   ```text
   gem collect search
   gem collect enrich
   gem validate
   gem clean
   gem transform
   gem mine-rules
   gem cluster
   gem report
   gem run
   gem status
   ```

7. Return non-zero exit codes and clear error messages on failure.

### Deliverables

- import-safe modules; and
- the `gem` command-line entry point.

### Complete when

- importing any project module performs no collection or file writes;
- the offline fixture pipeline produces deterministic results through the new
  package; and
- each command has useful `--help` output.

---

## Step 2 — Introduce validated configuration and run manifests

### Goal

Make every collection and analysis reproducible while allowing several
datasets to coexist safely.

### Work

1. Replace scattered environment settings with a versioned YAML or TOML
   configuration file.
2. Keep secrets such as tokens in environment variables, never in the config.
3. Validate types, ranges, incompatible values, and GitHub's API constraints.
4. Allow CLI arguments to override non-secret configuration values.
5. Assign a unique run ID to each collection.
6. Create a run manifest containing:
   - the normalized configuration and its hash;
   - collection start and completion times;
   - Git commit when available;
   - Python and dependency versions;
   - GitHub API version;
   - random seeds;
   - artifact paths and hashes; and
   - stage status and row counts.
7. Keep the current protection against mixing collection configurations, but
   isolate configurations by run instead of forcing the user to delete data.
8. Add explicit `--run-id` and `--resume` behavior.

### Deliverables

- `config.example.yml` or an equivalent example;
- validated configuration models;
- one manifest per run; and
- commands for listing and inspecting runs.

### Complete when

- two different queries can be collected without mixing their data;
- the exact configuration for any artifact can be identified; and
- invalid settings fail before any API request or file write occurs.

---

## Step 3 — Add a database-backed collection model

### Goal

Make collection resumable, queryable, and scalable while retaining immutable
raw evidence.

### Work

1. Introduce SQLite first; evaluate DuckDB later for analytical workloads.
2. Define tables for:
   - runs;
   - repositories and repository snapshots;
   - search slices;
   - raw API responses or response references;
   - enrichment status;
   - technology evidence;
   - collection errors; and
   - API call logs.
3. Use transactions so a completed batch is either fully recorded or not
   recorded at all.
4. Keep raw JSON responses immutable, either in the database or in a run-scoped
   raw directory referenced by the database.
5. Add schema migrations and a schema version.
6. Make repeated collection idempotent.
7. Add export commands for CSV and Parquet.
8. Never treat `not_found`, `timeout`, `permission_denied`, and generic errors
   as the same state.

### Deliverables

- database schema and migration mechanism;
- a repository layer that hides SQL from mining code; and
- import/export support for existing JSON and CSV artifacts.

### Complete when

- interruption after any batch can be resumed without duplicated records;
- all collected evidence can be traced to its run and repository; and
- the old baseline data can be imported without recollection.

---

## Step 4 — Build a production-grade GitHub API client

### Goal

Handle GitHub quotas and transient failures automatically and correctly.

### Work

1. Support REST and GraphQL through one client interface.
2. Separate pure retry decisions from network operations so they are easy to
   test.
3. Correctly distinguish:
   - primary rate limits;
   - secondary rate limits;
   - permission failures;
   - authentication failures;
   - invalid queries;
   - not-found responses;
   - network failures; and
   - GitHub 5xx failures.
4. Respect `Retry-After` and quota reset timestamps.
5. Wait automatically for long quota resets when configured to do so.
6. Support optional token rotation without logging token values.
7. Pace search calls according to the relevant quota.
8. Log endpoint, status, duration, retry count, rate-limit state, and GraphQL
   cost for every request.
9. Detect HTTP 200 GraphQL responses containing errors or incomplete data.
10. Add request timeouts, exponential backoff with jitter, and bounded retry
    limits.
11. Add conditional requests with ETags where useful.

### Deliverables

- a fully tested REST/GraphQL client;
- structured API-call logs; and
- a `gem status` view of progress, quotas, failures, and estimated remaining
  work.

### Complete when

- every important error path has a test;
- no permission error is mistaken for a rate limit;
- collection can survive temporary network and GitHub failures; and
- secrets never appear in logs or exceptions.

---

## Step 5 — Scale repository discovery beyond 1,000 results

### Goal

Collect a complete, well-defined sample rather than only GitHub's first 1,000
search results.

### Work

1. Split large searches recursively by star range or creation/update date.
2. Continue splitting until every slice contains no more than 1,000 results.
3. Save every slice and its completion state.
4. Deduplicate repositories by stable GitHub repository ID.
5. Record every query or topic that discovered each repository.
6. Check and react to GitHub's `incomplete_results` flag.
7. Use deterministic sorting and document the sampling method.
8. Support maximum-repository limits for smoke tests without marking partial
   slices as complete.
9. Detect repositories that move between result pages during long runs.
10. Produce a collection coverage report.

### Deliverables

- a resumable search slicer;
- search-coverage metrics; and
- tests for slice boundaries, overlap, deduplication, partial runs, and resume.

### Complete when

- a query returning several thousand repositories can be collected without
  silently stopping at 1,000; and
- rerunning discovery does not duplicate or lose known repositories.

---

## Step 6 — Collect rich technology evidence efficiently

### Goal

Measure the actual technology ecosystem rather than programming-language
presence alone.

### Work

1. Use batched GraphQL enrichment where possible.
2. Reduce the GraphQL batch size after heavy-query failures and restore it
   gradually after successful requests.
3. Continue collecting language byte counts, but calculate percentage shares.
4. Collect dependency manifests, initially:
   - `requirements.txt`;
   - `pyproject.toml`;
   - `setup.py`;
   - `setup.cfg`; and
   - `environment.yml`.
5. Add manifests for other ecosystems in later iterations, such as:
   - `package.json`;
   - `Cargo.toml`;
   - `go.mod`;
   - Maven and Gradle files; and
   - common .NET manifests.
6. Detect tooling from repository files and directories, including Docker,
   GitHub Actions, pre-commit, Terraform, Kubernetes, Make, documentation, and
   packaging tools.
7. Collect repository metadata useful for analysis: topics, license, dates,
   releases, stars, forks, issues, default branch, and archived/fork status.
8. Store each observation as evidence rather than immediately flattening it:

   ```text
   repository_id | technology | category | source | raw_value | confidence
   ```

9. Record truncation, missing files, unsupported formats, and parser status.
10. Make enrichers pluggable so a new ecosystem does not require rewriting the
    collector.

### Deliverables

- batched repository enrichment;
- an extensible technology-evidence model; and
- initial language, Python dependency, and tooling enrichers.

### Complete when

- thousands of repositories can be enriched with substantially fewer than one
  request per repository;
- every detected technology has a source; and
- language-only analysis remains available as a configured mode.

---

## Step 7 — Build an auditable cleaning and normalization layer

### Goal

Turn raw evidence into trustworthy technology baskets without hiding cleaning
decisions.

### Work

1. Implement and test a parser for every supported manifest format.
2. Normalize package names using ecosystem standards such as Python's PEP 503.
3. Maintain reviewed alias maps for equivalent distribution names.
4. Classify technology evidence as direct, transitive, tooling, language, or
   uncertain where possible.
5. Detect and flag frozen environment exports rather than treating every
   installed transitive package as a deliberate choice.
6. Detect resource repositories such as lists, tutorials, courses, books, and
   homework projects.
7. Add configurable rules for:
   - minimum language share;
   - minimum technology evidence;
   - archived and fork handling;
   - minimum activity or popularity; and
   - unusually large baskets.
8. Deduplicate technologies within each repository while retaining all
   supporting evidence.
9. Produce a cleaning decision table containing the original value, normalized
   value, action, and reason.
10. Produce a repository cleaning funnel with counts at every step.
11. Never silently drop a repository or technology.

### Deliverables

- tested parsers and canonicalization rules;
- cleaned repository and technology tables;
- a machine-readable cleaning audit; and
- a human-readable cleaning report.

### Complete when

- every removal and rename has an explicit reason;
- rerunning cleaning is deterministic; and
- manual review of a sample can trace each technology back to raw evidence.

---

## Step 8 — Upgrade association-rule and stack mining

### Goal

Find reliable, non-trivial technology relationships and interpretable stacks.

### Work

1. Replace pair-only Apriori with configurable FP-Growth.
2. Support itemsets of up to three or four technologies.
3. Evaluate several support thresholds and record the resulting itemset and
   rule counts before selecting a default.
4. Calculate support count, support, confidence, lift, leverage, conviction,
   and relevant confidence intervals where practical.
5. Prefer a single consequent for readable rules.
6. Remove redundant longer rules unless they materially improve confidence.
7. Flag obvious same-family rules rather than mixing them with cross-technology
   findings.
8. Identify potential substitutes using lift below one.
9. Build a weighted rule network and detect technology communities with a
   reproducible community algorithm.
10. Preserve the exact filters, thresholds, and random seeds in the run
    manifest.
11. Export both machine-readable results and concise human-readable summaries.

### Deliverables

- frequent itemsets;
- pruned association rules;
- substitute comparisons;
- technology communities/stacks; and
- threshold-evaluation outputs.

### Complete when

- rules can contain meaningful multi-technology antecedents;
- redundant and obvious rules are distinguishable from useful findings; and
- every reported rule can be reproduced from a recorded run.

---

## Step 9 — Upgrade clustering and cluster interpretation

### Goal

Discover stable project families using the full technology basket rather than
only raw language presence.

### Work

1. Build a sparse project-by-technology matrix.
2. Correct author bias by allowing a configurable maximum number of projects
   per owner.
3. Exclude or separately analyze repositories with insufficient evidence and
   abnormal baskets.
4. Compare binary, TF-IDF, and category-aware feature weighting.
5. Use dimensionality reduction such as Truncated SVD before scalable
   clustering.
6. Compare at least:
   - K-Means;
   - hierarchical clustering; and
   - HDBSCAN when its noise model is useful.
7. Evaluate candidate cluster counts with:
   - silhouette score;
   - stability across random seeds;
   - bootstrap stability;
   - adjusted Rand index between methods;
   - cluster-size balance; and
   - interpretability.
8. Select clusters using a documented rule, not only the highest silhouette.
9. Explain each cluster using:
   - over-represented technologies;
   - representative repositories near the center;
   - median age, stars, and activity;
   - test, CI, container, and documentation adoption; and
   - uncertainty or membership strength.
10. Generate editable family names instead of treating automated labels as
    ground truth.
11. Clearly state when clusters overlap or are weakly separated.

### Deliverables

- cluster-evaluation tables;
- assignments and cluster profiles;
- representative repositories;
- stability results; and
- two-dimensional diagnostic visualizations.

### Complete when

- the chosen clustering is stable under reasonable reruns;
- cluster selection has a recorded justification; and
- every family can be explained with concrete evidence.

---

## Step 10 — Add analytical robustness and temporal analysis

### Goal

Determine whether findings are stable, generalizable within the selected
scope, and changing over time.

### Work

1. Re-run rule mining with several support and lift thresholds.
2. Compare results with and without an owner cap.
3. Bootstrap repositories and measure rule and cluster stability.
4. Compare high-star and medium-star samples.
5. Compare older and newer repositories.
6. Compare results between collection snapshots.
7. Track emerging, stable, and declining technologies.
8. Analyze how technology stacks and project families change over time.
9. Distinguish repository creation time from evidence observation time; current
   dependency files must not be presented as historical dependencies at
   creation time.
10. Record which conclusions are robust and which depend strongly on sampling
    or thresholds.

### Deliverables

- robustness tables;
- stable-rule and unstable-rule indicators;
- temporal trend outputs; and
- a limitations section generated from run metadata.

### Complete when

- major conclusions have at least one robustness check; and
- temporal claims do not confuse current repository state with past state.

---

## Step 11 — Expand tests, schemas, and continuous integration

### Goal

Make correctness enforceable rather than dependent on manual checking.

### Work

1. Add unit tests for configuration, parsers, canonicalization, filters,
   retry decisions, feature building, rule pruning, and cluster selection.
2. Add mocked REST and GraphQL integration tests.
3. Add tests for GraphQL partial errors and HTTP 200 invalid bodies.
4. Test interruption and resume at search, enrichment, and export boundaries.
5. Test duplicate pages, changing result pages, deleted repositories, and
   partial collections.
6. Add database migration and idempotency tests.
7. Validate stage inputs and outputs with explicit schemas.
8. Add invariants such as unique IDs, non-negative counts, valid timestamps,
   and matrix-to-basket consistency.
9. Add golden tests for selected fixture rules and clusters.
10. Add an end-to-end test that requires no network access.
11. Configure continuous integration for:
    - tests;
    - formatting;
    - linting;
    - static typing;
    - dependency/security checks; and
    - the fixture smoke pipeline.

### Deliverables

- comprehensive automated tests;
- schema validation at stage boundaries; and
- a GitHub Actions workflow.

### Complete when

- critical collection and analytical paths are covered;
- the full fixture pipeline runs in CI; and
- malformed intermediate data fails with a clear stage-specific message.

---

## Step 12 — Generate reports and an optional explorer

### Goal

Turn pipeline outputs into a reproducible research deliverable and an
accessible exploration tool.

### Work

1. Add `gem report --run-id ...`.
2. Generate an HTML report containing:
   - collection scope and API cost;
   - data-quality and cleaning funnel;
   - technology popularity;
   - co-occurrence analysis;
   - association rules and technology communities;
   - cluster selection and family profiles;
   - robustness and temporal findings; and
   - limitations and reproducibility metadata.
3. Add optional PDF export.
4. Ensure every figure is generated from recorded artifacts, not manually
   edited data.
5. Add an optional lightweight explorer for filtering repositories, inspecting
   evidence, searching rules, comparing runs, and changing display thresholds.
6. Keep analytical thresholds controlled by the pipeline configuration rather
   than silently recalculating results in the UI.

### Deliverables

- reproducible HTML report;
- optional PDF report;
- figure-generation code; and
- optional interactive explorer.

### Complete when

- a user can understand the methods, findings, and limitations from the report;
- every result links back to a run and configuration; and
- regenerating the report does not require GitHub access.

---

## Step 13 — Optimize, package, and release the project

### Goal

Make the finished system fast, installable, documented, and maintainable.

### Work

1. Profile collection, parsing, matrix construction, mining, and clustering.
2. Use Parquet and sparse matrices where they materially improve performance.
3. Cache parsing by content hash.
4. Avoid full pairwise distance matrices for large datasets.
5. Add bounded concurrency where batching is unavailable.
6. Add structured logs and stage timing/memory metrics.
7. Define the package and CLI in `pyproject.toml`.
8. Pin or lock dependencies and document supported Python versions.
9. Write installation, quick-start, architecture, configuration, extension,
   and troubleshooting documentation.
10. Add contribution guidelines and release notes.
11. Create a versioned release only after the fixture pipeline and CI pass.

### Deliverables

- installable Python package;
- locked dependencies;
- performance benchmarks;
- complete user and developer documentation; and
- a versioned release.

### Complete when

- a new user can install the package and run the fixture workflow from the
  documentation;
- a full run reports its resource usage and timing; and
- releases are reproducible from a tagged commit.

---

## Definition of “best”

The project is not finished merely because it generates more rules, clusters,
or charts than another implementation. It is successful when it is:

- **accurate:** technology evidence is parsed and normalized correctly;
- **traceable:** every result can be followed back to raw evidence;
- **reproducible:** a run records its configuration, code, seeds, and inputs;
- **resumable:** interruption does not corrupt or lose collection progress;
- **scalable:** thousands of repositories can be collected and analyzed;
- **general:** new ecosystems and evidence sources can be added as plugins;
- **auditable:** cleaning and sampling decisions are explicit;
- **statistically responsible:** findings include stability checks and limits;
- **tested:** important failure modes and transformations are automated; and
- **usable:** the CLI, documentation, and reports make the system approachable.

## Completion note

The implementation roadmap is complete in GitHub Ecosystem Miner 1.0.0. The optional interactive explorer was
not required because the offline HTML report fulfills the reporting deliverable. Publishing a Git
tag or GitHub release remains a repository-owner action after the release commit is reviewed and
pushed.
