# GitHub Ecosystem Miner

GitHub Ecosystem Miner collects a reproducible GitHub repository sample,
extracts technology evidence, audits cleaning decisions, and mines association
rules and technology communities.

The application uses an installable Python package, an isolated DuckDB database
for each run, and a single `gem` command-line interface.

## Features

- Recursive star-range slicing beyond GitHub Search's 1,000-result limit
- Adaptive optimized-REST or batched GraphQL enrichment with REST fallback
- Recursive Git Trees discovery, commit-pinned raw/blob retrieval, and safe optional archives
- Persistent tree/file caching and resumable repository checkpoints in DuckDB
- Separate REST core, REST search and GraphQL budgets with secondary-limit handling
- Isolated runs protected by configuration fingerprints
- Technology evidence from dependency files, repository tooling, and languages
- Auditable parsing, normalization, filtering, and repository inclusion
- FP-Growth association rules with threshold evaluation and redundancy pruning
- Substitute detection and Louvain technology communities
- Stable project-family clustering across binary, TF-IDF and category-aware evidence
- K-Means, hierarchical and HDBSCAN comparison with explainable cluster profiles
- Bootstrap, owner-bias, popularity-cohort and collected-snapshot robustness checks
- Reproducible offline HTML reports and CSV/Parquet exports
- Mocked end-to-end tests that do not use GitHub quota

## Project structure

```text
github-ecosystem-miner/
├── .github/workflows/ci.yml
├── data/runs/                  # generated run databases and manifests
├── src/github_ecosystem_miner/
│   ├── association.py
│   ├── benchmark.py
│   ├── cleaning.py
│   ├── clustering.py
│   ├── cli.py
│   ├── collection.py
│   ├── config.py
│   ├── errors.py
│   ├── github_client.py
│   ├── rate_limits.py
│   ├── retrieval.py
│   ├── normalization.py
│   ├── parsers.py
│   ├── reporting.py
│   ├── robustness.py
│   ├── runs.py
│   └── storage.py
├── tests/
├── .env.example
├── config.toml
├── docs/COLLECTION_ARCHITECTURE.md
├── requirements.lock
├── IMPROVEMENT_ROADMAP.md
└── pyproject.toml
```

Generated folders such as `.venv`, `__pycache__`, `*.egg-info`, and
`.ruff_cache` are local artifacts and are ignored by Git.

## Installation

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
gem --help
```

`pyproject.toml` is the authoritative dependency and package definition.

## Configuration

Non-secret settings are stored in `config.toml`.

A base query must not contain `stars:` because the collector manages star
ranges itself:

```toml
[ecosystem]
queries = ["topic:webapp fork:false archived:false"]
minimum_stars = 0
maximum_stars = 1000000
```

Copy `.env.example` to `.env` and add a GitHub token:

```powershell
Copy-Item .env.example .env
```

```dotenv
GITHUB_TOKEN=github_pat_xxx
# Optional token rotation:
# GITHUB_TOKENS=github_pat_one,github_pat_two
```

Never commit `.env`.

Important collection controls include `enrichment_strategy`, tree fallback bounds, file-size
limits, quota reserves and archive safety limits. Archives are disabled by default because the live
sample benchmark found selective retrieval more bandwidth-efficient.

## Running the pipeline

Create an isolated run:

```powershell
gem run create
```

Use the returned run ID for every stage:

```powershell
gem collect search --run-id <RUN_ID>
gem collect enrich --run-id <RUN_ID>
gem clean --run-id <RUN_ID>
gem mine-rules --run-id <RUN_ID>
gem cluster --run-id <RUN_ID>
gem robustness --run-id <RUN_ID>
gem report --run-id <RUN_ID>
gem run status --run-id <RUN_ID>
```

For an incremental update, check the repositories' current commit SHAs and reuse cached trees and
blobs wherever identities are unchanged:

```powershell
gem collect enrich --run-id <RUN_ID> --refresh
```

Compare the three collection strategies on the same discovered repositories:

```powershell
gem benchmark-collection --run-id <RUN_ID> --limit 3
```

Results are saved in DuckDB. See
[docs/COLLECTION_ARCHITECTURE.md](docs/COLLECTION_ARCHITECTURE.md) for the audit, fallback behavior,
cache keys, archive safeguards and benchmark interpretation. The measured sample is recorded in
[docs/BENCHMARK_RESULTS.md](docs/BENCHMARK_RESULTS.md).

To create and execute a complete run:

```powershell
gem pipeline
```

Each run is stored under `data/runs/<RUN_ID>/`:

```text
manifest.json       configuration, fingerprint, versions, and stage status
ecosystem.duckdb    repositories, evidence, audits, API logs, and results
```

An existing run refuses to resume when the active configuration has a different
fingerprint.

Export every available run table without GitHub access:

```powershell
gem export --run-id <RUN_ID> --format parquet
gem export --run-id <RUN_ID> --format csv
```

## Technology evidence

Every accepted observation retains its provenance:

```text
repository_id | technology | category | source | raw_value | confidence | directness
```

Supported dependency formats currently include:

- `requirements.txt`
- `pyproject.toml`
- `setup.py`
- `setup.cfg`
- `environment.yml`

Cleaning decisions are stored in `cleaning_audit`, including accepted, renamed,
dropped, and unsupported observations with their reasons. Repository inclusion
decisions are stored in `repository_cleaning`.

## Association mining

The mining stage stores:

- frequent itemsets and support counts;
- rules with confidence, lift, leverage, and conviction;
- redundant and same-family flags;
- support-threshold evaluations;
- low-lift substitute candidates; and
- reproducible Louvain technology communities.

Thresholds are configured in the `[mining]` section of `config.toml`.

## Project families and robustness

`gem cluster` builds a sparse repository-by-technology matrix, applies an owner cap, compares three
feature weightings and evaluates K-Means, hierarchical clustering and HDBSCAN where applicable.
Selection considers silhouette, stability and cluster-size balance. Each family stores
over-represented technologies, representative repositories and descriptive metadata.

`gem robustness` measures rule bootstrap frequency, support-threshold sensitivity, owner-cap bias,
high/medium-star agreement, cluster stability and technology prevalence across creation cohorts.
When repeated collected commits exist, it also compares preserved evidence snapshots. Creation-time
cohorts are explicitly labelled as current observations, not historical dependencies.

## Reports

`gem report` produces `data/runs/<RUN_ID>/report.html` entirely from the recorded DuckDB run. It
includes collection cost, cleaning decisions, technologies, rules, project families, robustness,
temporal caveats, limitations and the full reproducibility manifest.

## Tests and code quality

```powershell
python -m unittest discover -s tests -v
python -m ruff check src/github_ecosystem_miner tests
```

The test suite uses mocked GitHub responses and temporary DuckDB databases. It
does not require a token or network access. GitHub Actions runs the same checks
on pushes and pull requests.

## Documentation

See [architecture](docs/ARCHITECTURE.md),
[collection decisions](docs/COLLECTION_ARCHITECTURE.md),
[benchmark results](docs/BENCHMARK_RESULTS.md), and
[troubleshooting](docs/TROUBLESHOOTING.md). The original implementation roadmap is retained in
[IMPROVEMENT_ROADMAP.md](IMPROVEMENT_ROADMAP.md) with completion status.
