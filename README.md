# GitHub Ecosystem Miner

GitHub Ecosystem Miner collects a reproducible GitHub repository sample,
extracts technology evidence, audits cleaning decisions, and mines association
rules and technology communities.

The application uses an installable Python package, an isolated DuckDB database
for each run, and a single `gem` command-line interface.

## Features

- Recursive star-range slicing beyond GitHub Search's 1,000-result limit
- Batched GraphQL enrichment
- Automatic retry, quota handling, and optional token rotation
- Isolated runs protected by configuration fingerprints
- Technology evidence from dependency files, repository tooling, and languages
- Auditable parsing, normalization, filtering, and repository inclusion
- FP-Growth association rules with threshold evaluation and redundancy pruning
- Substitute detection and Louvain technology communities
- Mocked end-to-end tests that do not use GitHub quota

## Project structure

```text
github-ecosystem-miner/
├── .github/workflows/ci.yml
├── data/runs/                  # generated run databases and manifests
├── src/github_ecosystem_miner/
│   ├── association.py
│   ├── cleaning.py
│   ├── cli.py
│   ├── collection.py
│   ├── config.py
│   ├── errors.py
│   ├── github_client.py
│   ├── normalization.py
│   ├── parsers.py
│   ├── runs.py
│   └── storage.py
├── tests/
├── .env.example
├── config.toml
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
gem run status --run-id <RUN_ID>
```

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

## Tests and code quality

```powershell
python -m unittest discover -s tests -v
python -m ruff check src/github_ecosystem_miner tests
```

The test suite uses mocked GitHub responses and temporary DuckDB databases. It
does not require a token or network access. GitHub Actions runs the same checks
on pushes and pull requests.

## Roadmap

See [IMPROVEMENT_ROADMAP.md](IMPROVEMENT_ROADMAP.md). The next major capability
is upgraded clustering and cluster interpretation.
