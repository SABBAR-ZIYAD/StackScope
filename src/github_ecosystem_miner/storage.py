from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

SCHEMA_VERSION = 1

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
    endpoint VARCHAR NOT NULL,
    status INTEGER,
    duration_ms DOUBLE,
    attempt INTEGER NOT NULL,
    token_index INTEGER NOT NULL,
    remaining BIGINT,
    reset_at BIGINT,
    graphql_cost BIGINT,
    error VARCHAR
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
"""


def initialize_database(path: str | Path) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_path)) as connection:
        connection.execute(SCHEMA_SQL)
        current = connection.execute("SELECT MAX(version) FROM schema_info").fetchone()[0]
        if current is None:
            connection.execute("INSERT INTO schema_info(version) VALUES (?)", [SCHEMA_VERSION])
        elif current != SCHEMA_VERSION:
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
