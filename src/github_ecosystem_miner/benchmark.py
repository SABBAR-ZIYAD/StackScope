from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from github_ecosystem_miner.collection import (
    RepositorySnapshot,
    _graphql_snapshots,
    _rest_snapshot,
)
from github_ecosystem_miner.errors import CollectionError
from github_ecosystem_miner.github_client import GitHubClient
from github_ecosystem_miner.progress import get_logger
from github_ecosystem_miner.retrieval import discover_tree, retrieve_relevant_files
from github_ecosystem_miner.runs import RunContext
from github_ecosystem_miner.storage import connect, initialize_database

LOGGER = get_logger("benchmark")


@dataclass(frozen=True)
class BenchmarkResult:
    strategy: str
    repository_count: int
    analyzed_repositories: int
    rest_core_requests: int
    rest_search_requests: int
    graphql_requests: int
    graphql_points: int
    bytes_transferred: int
    rate_limit_errors: int
    wall_clock_seconds: float


def benchmark_collection(
    context: RunContext, client: GitHubClient, *, limit: int | None = None
) -> dict[str, Any]:
    requested = limit or context.config.collection.benchmark_repository_limit
    with connect(context.database_path, read_only=True) as connection:
        repositories = connection.execute(
            """
            SELECT repository_id, full_name, default_branch, size_kb
            FROM repositories WHERE run_id = ?
            ORDER BY stars DESC, repository_id LIMIT ?
            """,
            [context.run_id, requested],
        ).fetchall()
    if not repositories:
        raise CollectionError("Run search before benchmarking the collection strategies")

    benchmark_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    LOGGER.info("[benchmark] Comparing strategies on %d repositories", len(repositories))
    results = []
    for strategy in ("existing_rest_only", "optimized_rest_only", "hybrid"):
        LOGGER.info("[benchmark] Running %s", strategy)
        result = _run_strategy(context, client, repositories, strategy)
        LOGGER.info(
            "[benchmark] %s: %d analyzed, %d core requests, %.3f seconds",
            strategy,
            result.analyzed_repositories,
            result.rest_core_requests,
            result.wall_clock_seconds,
        )
        results.append(result)
    with connect(context.database_path) as connection:
        for result in results:
            connection.execute(
                """
                INSERT OR REPLACE INTO collection_benchmarks
                    (benchmark_id, strategy, repository_count, analyzed_repositories,
                     rest_core_requests, rest_search_requests, graphql_requests,
                     graphql_points, bytes_transferred, rate_limit_errors,
                     wall_clock_seconds, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    benchmark_id,
                    *asdict(result).values(),
                    json.dumps(asdict(result), sort_keys=True),
                ],
            )
    successful = [result for result in results if result.analyzed_repositories]
    best = (
        min(
            successful,
            key=lambda item: (
                -item.analyzed_repositories,
                item.rate_limit_errors,
                item.rest_core_requests + item.graphql_requests,
                item.wall_clock_seconds,
            ),
        )
        if successful
        else None
    )
    return {
        "benchmark_id": benchmark_id,
        "recommended_strategy": best.strategy if best else None,
        "results": [asdict(result) for result in results],
    }


def _run_strategy(
    context: RunContext,
    client: GitHubClient,
    repositories: list[tuple[Any, ...]],
    strategy: str,
) -> BenchmarkResult:
    calls: list[dict[str, Any]] = []
    old_logger = client.logger
    client.logger = calls.append
    analyzed = 0
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="github-ecosystem-miner-benchmark-") as directory:
            database_path = Path(directory) / "cache.duckdb"
            initialize_database(database_path)
            if strategy == "existing_rest_only":
                analyzed = _benchmark_legacy(context, client, repositories)
            else:
                snapshots: dict[int, RepositorySnapshot] = {}
                if strategy == "hybrid" and client.authenticated:
                    try:
                        snapshots, _ = _graphql_snapshots(client, repositories)
                    except CollectionError:
                        snapshots = {}
                for row in repositories:
                    repository_id, full_name, default_branch, size_kb = row
                    try:
                        snapshot = snapshots.get(int(repository_id)) or _rest_snapshot(
                            client,
                            int(repository_id),
                            str(full_name),
                            str(default_branch or "HEAD"),
                        )
                        tree = discover_tree(
                            database_path,
                            client,
                            repository_id=int(repository_id),
                            full_name=str(full_name),
                            commit_sha=snapshot.commit_sha,
                            tree_sha=snapshot.tree_sha,
                            config=context.config.collection,
                        )
                        retrieve_relevant_files(
                            database_path,
                            client,
                            repository_id=int(repository_id),
                            full_name=str(full_name),
                            commit_sha=snapshot.commit_sha,
                            repository_size_kb=int(size_kb or 0),
                            entries=tree.entries,
                            dependency_files=context.config.evidence.dependency_files,
                            config=context.config.collection,
                        )
                        analyzed += 1
                    except CollectionError:
                        continue
    finally:
        client.logger = old_logger
    elapsed = time.perf_counter() - started
    return _summarize(strategy, len(repositories), analyzed, calls, elapsed)


def _benchmark_legacy(
    context: RunContext, client: GitHubClient, repositories: list[tuple[Any, ...]]
) -> int:
    analyzed = 0
    for _, full_name, default_branch, _ in repositories:
        try:
            # The legacy design redundantly fetched metadata already returned by Search.
            client.rest_get(f"/repos/{full_name}", resource="core")
            client.rest_get(f"/repos/{full_name}/languages", resource="core")
            client.rest_get(f"/repos/{full_name}/commits/{default_branch}", resource="core")
            for path in context.config.evidence.dependency_files:
                client.rest_get(
                    f"/repos/{full_name}/contents/{path}",
                    params={"ref": default_branch},
                    resource="core",
                    allow_not_found=True,
                )
            analyzed += 1
        except CollectionError:
            continue
    return analyzed


def _summarize(
    strategy: str,
    repository_count: int,
    analyzed: int,
    calls: list[dict[str, Any]],
    elapsed: float,
) -> BenchmarkResult:
    return BenchmarkResult(
        strategy=strategy,
        repository_count=repository_count,
        analyzed_repositories=analyzed,
        rest_core_requests=sum(
            1 for row in calls if row.get("api") == "rest" and row.get("resource") == "core"
        ),
        rest_search_requests=sum(
            1 for row in calls if row.get("api") == "rest" and row.get("resource") == "search"
        ),
        graphql_requests=sum(1 for row in calls if row.get("api") == "graphql"),
        graphql_points=sum(int(row.get("graphql_cost") or 0) for row in calls),
        bytes_transferred=sum(int(row.get("bytes_transferred") or 0) for row in calls),
        rate_limit_errors=sum(
            1
            for row in calls
            if row.get("status") in {403, 429} and "rate" in str(row.get("error", ""))
        ),
        wall_clock_seconds=round(elapsed, 3),
    )
