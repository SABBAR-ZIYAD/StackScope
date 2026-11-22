from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY,
    config_hash VARCHAR NOT NULL,
    config_json VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    git_commit VARCHAR,
    python_version VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_status (
    run_id VARCHAR NOT NULL,
    stage VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    row_count BIGINT,
    message VARCHAR,
    PRIMARY KEY (run_id, stage)
);

CREATE TABLE IF NOT EXISTS search_slices (
    run_id VARCHAR NOT NULL,
    base_query VARCHAR NOT NULL,
    star_min BIGINT NOT NULL,
    star_max BIGINT NOT NULL,
    total_count BIGINT,
    fetched_count BIGINT NOT NULL DEFAULT 0,
    status VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, base_query, star_min, star_max)
);

CREATE TABLE IF NOT EXISTS repositories (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    node_id VARCHAR,
    full_name VARCHAR NOT NULL,
    owner VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    description VARCHAR,
    html_url VARCHAR,
    primary_language VARCHAR,
    topics_json VARCHAR NOT NULL,
    found_by_queries_json VARCHAR NOT NULL,
    stars BIGINT NOT NULL,
    forks BIGINT NOT NULL,
    watchers BIGINT NOT NULL,
    open_issues BIGINT NOT NULL,
    size_kb BIGINT NOT NULL,
    license VARCHAR,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    pushed_at TIMESTAMP,
    default_branch VARCHAR,
    is_fork BOOLEAN NOT NULL,
    is_archived BOOLEAN NOT NULL,
    collected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, repository_id)
);

CREATE TABLE IF NOT EXISTS repository_enrichment (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    full_name VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    commit_sha VARCHAR,
    tree_sha VARCHAR,
    retrieval_strategy VARCHAR,
    languages_json VARCHAR,
    root_files_json VARCHAR,
    workflows_json VARCHAR,
    manifests_json VARCHAR,
    graphql_errors_json VARCHAR,
    error_message VARCHAR,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, repository_id)
);

CREATE TABLE IF NOT EXISTS technology_evidence (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    technology VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    raw_value VARCHAR,
    confidence DOUBLE NOT NULL,
    directness VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, repository_id, technology, category, source)
);

CREATE TABLE IF NOT EXISTS cleaning_audit (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    source VARCHAR NOT NULL,
    raw_value VARCHAR,
    normalized_value VARCHAR,
    action VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repository_cleaning (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    kept BOOLEAN NOT NULL,
    reason VARCHAR NOT NULL,
    package_count BIGINT NOT NULL,
    technology_count BIGINT NOT NULL,
    is_resource BOOLEAN NOT NULL,
    PRIMARY KEY (run_id, repository_id)
);

CREATE TABLE IF NOT EXISTS api_calls (
    run_id VARCHAR NOT NULL,
    requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    api VARCHAR NOT NULL,
    resource VARCHAR,
    endpoint VARCHAR NOT NULL,
    status INTEGER,
    duration_ms DOUBLE,
    attempt INTEGER NOT NULL,
    token_index INTEGER NOT NULL,
    remaining BIGINT,
    reset_at BIGINT,
    graphql_cost BIGINT,
    bytes_transferred BIGINT,
    error VARCHAR
);

CREATE TABLE IF NOT EXISTS repository_snapshots (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    commit_sha VARCHAR NOT NULL,
    tree_sha VARCHAR NOT NULL,
    languages_json VARCHAR,
    source VARCHAR NOT NULL,
    collected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, repository_id, commit_sha)
);

CREATE TABLE IF NOT EXISTS repository_tree_cache (
    repository_id BIGINT NOT NULL,
    commit_sha VARCHAR NOT NULL,
    tree_sha VARCHAR NOT NULL,
    entries_json VARCHAR NOT NULL,
    truncated BOOLEAN NOT NULL,
    api_requests BIGINT NOT NULL,
    bytes_transferred BIGINT NOT NULL,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repository_id, commit_sha, tree_sha)
);

CREATE TABLE IF NOT EXISTS repository_file_cache (
    repository_id BIGINT NOT NULL,
    commit_sha VARCHAR NOT NULL,
    path VARCHAR NOT NULL,
    blob_sha VARCHAR NOT NULL,
    content BLOB NOT NULL,
    byte_size BIGINT NOT NULL,
    retrieval_source VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repository_id, commit_sha, path, blob_sha)
);

CREATE TABLE IF NOT EXISTS enrichment_metrics (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    commit_sha VARCHAR NOT NULL,
    strategy VARCHAR NOT NULL,
    tree_requests BIGINT NOT NULL,
    file_requests BIGINT NOT NULL,
    cache_hits BIGINT NOT NULL,
    bytes_transferred BIGINT NOT NULL,
    archive_used BOOLEAN NOT NULL,
    relevant_files BIGINT NOT NULL,
    duration_ms DOUBLE NOT NULL,
    PRIMARY KEY (run_id, repository_id, commit_sha)
);

CREATE TABLE IF NOT EXISTS collection_benchmarks (
    benchmark_id VARCHAR NOT NULL,
    strategy VARCHAR NOT NULL,
    repository_count BIGINT NOT NULL,
    analyzed_repositories BIGINT NOT NULL,
    rest_core_requests BIGINT NOT NULL,
    rest_search_requests BIGINT NOT NULL,
    graphql_requests BIGINT NOT NULL,
    graphql_points BIGINT NOT NULL,
    bytes_transferred BIGINT NOT NULL,
    rate_limit_errors BIGINT NOT NULL,
    wall_clock_seconds DOUBLE NOT NULL,
    details_json VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (benchmark_id, strategy)
);

CREATE TABLE IF NOT EXISTS frequent_itemsets (
    run_id VARCHAR NOT NULL,
    items_json VARCHAR NOT NULL,
    item_count INTEGER NOT NULL,
    support DOUBLE NOT NULL,
    support_count BIGINT NOT NULL,
    PRIMARY KEY (run_id, items_json)
);

CREATE TABLE IF NOT EXISTS association_rules (
    run_id VARCHAR NOT NULL,
    antecedent_json VARCHAR NOT NULL,
    consequent VARCHAR NOT NULL,
    support DOUBLE NOT NULL,
    support_count BIGINT NOT NULL,
    confidence DOUBLE NOT NULL,
    lift DOUBLE NOT NULL,
    leverage DOUBLE NOT NULL,
    conviction DOUBLE,
    antecedent_count INTEGER NOT NULL,
    redundant BOOLEAN NOT NULL,
    same_family BOOLEAN NOT NULL,
    PRIMARY KEY (run_id, antecedent_json, consequent)
);

CREATE TABLE IF NOT EXISTS technology_communities (
    run_id VARCHAR NOT NULL,
    technology VARCHAR NOT NULL,
    community INTEGER NOT NULL,
    PRIMARY KEY (run_id, technology)
);

CREATE TABLE IF NOT EXISTS mining_threshold_evaluation (
    run_id VARCHAR NOT NULL,
    minimum_support DOUBLE NOT NULL,
    minimum_support_count BIGINT NOT NULL,
    frequent_itemsets BIGINT NOT NULL,
    confidence_rules BIGINT NOT NULL,
    lift_rules BIGINT NOT NULL,
    PRIMARY KEY (run_id, minimum_support)
);

CREATE TABLE IF NOT EXISTS technology_substitutes (
    run_id VARCHAR NOT NULL,
    technology_a VARCHAR NOT NULL,
    technology_b VARCHAR NOT NULL,
    support_a DOUBLE NOT NULL,
    support_b DOUBLE NOT NULL,
    joint_support DOUBLE NOT NULL,
    joint_count BIGINT NOT NULL,
    lift DOUBLE NOT NULL,
    PRIMARY KEY (run_id, technology_a, technology_b)
);

CREATE TABLE IF NOT EXISTS cluster_evaluations (
    run_id VARCHAR NOT NULL,
    method VARCHAR NOT NULL,
    weighting VARCHAR NOT NULL,
    cluster_count INTEGER NOT NULL,
    silhouette DOUBLE,
    stability DOUBLE,
    balance DOUBLE,
    selection_score DOUBLE,
    selected BOOLEAN NOT NULL,
    details_json VARCHAR NOT NULL,
    PRIMARY KEY (run_id, method, weighting, cluster_count)
);

CREATE TABLE IF NOT EXISTS repository_clusters (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    cluster_id INTEGER NOT NULL,
    membership DOUBLE,
    distance_to_center DOUBLE,
    PRIMARY KEY (run_id, repository_id)
);

CREATE TABLE IF NOT EXISTS cluster_profiles (
    run_id VARCHAR NOT NULL,
    cluster_id INTEGER NOT NULL,
    label VARCHAR NOT NULL,
    repository_count BIGINT NOT NULL,
    top_technologies_json VARCHAR NOT NULL,
    representative_repositories_json VARCHAR NOT NULL,
    metrics_json VARCHAR NOT NULL,
    PRIMARY KEY (run_id, cluster_id)
);

CREATE TABLE IF NOT EXISTS robustness_results (
    run_id VARCHAR NOT NULL,
    check_name VARCHAR NOT NULL,
    metric VARCHAR NOT NULL,
    value DOUBLE,
    details_json VARCHAR NOT NULL,
    PRIMARY KEY (run_id, check_name, metric)
);

CREATE TABLE IF NOT EXISTS temporal_trends (
    run_id VARCHAR NOT NULL,
    cohort VARCHAR NOT NULL,
    technology VARCHAR NOT NULL,
    prevalence DOUBLE NOT NULL,
    repository_count BIGINT NOT NULL,
    caveat VARCHAR NOT NULL,
    PRIMARY KEY (run_id, cohort, technology)
);

CREATE TABLE IF NOT EXISTS report_artifacts (
    run_id VARCHAR NOT NULL,
    format VARCHAR NOT NULL,
    relative_path VARCHAR NOT NULL,
    sha256 VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, format)
);

CREATE TABLE IF NOT EXISTS technology_evidence_snapshots (
    run_id VARCHAR NOT NULL,
    repository_id BIGINT NOT NULL,
    commit_sha VARCHAR NOT NULL,
    technology VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    confidence DOUBLE NOT NULL,
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, repository_id, commit_sha, technology, category, source)
);
"""

MIGRATION_2_SQL = """
ALTER TABLE repository_enrichment ADD COLUMN IF NOT EXISTS commit_sha VARCHAR;
ALTER TABLE repository_enrichment ADD COLUMN IF NOT EXISTS tree_sha VARCHAR;
ALTER TABLE repository_enrichment ADD COLUMN IF NOT EXISTS retrieval_strategy VARCHAR;
ALTER TABLE api_calls ADD COLUMN IF NOT EXISTS resource VARCHAR;
ALTER TABLE api_calls ADD COLUMN IF NOT EXISTS bytes_transferred BIGINT;
"""


def initialize_database(path: str | Path) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_path)) as connection:
        connection.execute(SCHEMA_SQL)
        current = connection.execute("SELECT MAX(version) FROM schema_info").fetchone()[0]
        if current is None:
            connection.execute("INSERT INTO schema_info(version) VALUES (?)", [SCHEMA_VERSION])
        else:
            if current == 1:
                connection.execute(MIGRATION_2_SQL)
                connection.execute("INSERT INTO schema_info(version) VALUES (2)")
                current = 2
            if current == 2:
                connection.execute("INSERT INTO schema_info(version) VALUES (3)")
                current = 3
        if current is not None and current != SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema {current} is incompatible with expected schema {SCHEMA_VERSION}"
            )


@contextmanager
def connect(path: str | Path, *, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect(str(path), read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()
