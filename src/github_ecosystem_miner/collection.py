from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from github_ecosystem_miner.errors import CollectionError, TransientCollectionError
from github_ecosystem_miner.github_client import GitHubClient
from github_ecosystem_miner.progress import get_logger, progress_message
from github_ecosystem_miner.retrieval import discover_tree, retrieve_relevant_files
from github_ecosystem_miner.runs import RunContext
from github_ecosystem_miner.storage import connect

LOGGER = get_logger("collection")


def build_search_query(base_query: str, star_min: int, star_max: int) -> str:
    return f"{base_query.strip()} stars:{star_min}..{star_max}".strip()


def split_star_range(star_min: int, star_max: int) -> tuple[tuple[int, int], tuple[int, int]]:
    if star_min >= star_max:
        raise ValueError("A single star value cannot be split further")
    midpoint = (star_min + star_max) // 2
    return (star_min, midpoint), (midpoint + 1, star_max)


def _api_logger(context: RunContext):
    def log(row: dict[str, Any]) -> None:
        with connect(context.database_path) as connection:
            connection.execute(
                """
                INSERT INTO api_calls
                    (run_id, api, resource, endpoint, status, duration_ms, attempt,
                     token_index, remaining, reset_at, graphql_cost, bytes_transferred, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    context.run_id,
                    row["api"],
                    row["resource"],
                    row["endpoint"],
                    row["status"],
                    row["duration_ms"],
                    row["attempt"],
                    row["token_index"],
                    row["remaining"],
                    row["reset_at"],
                    row["graphql_cost"],
                    row["bytes_transferred"],
                    row["error"],
                ],
            )

    return log


def collect_search(context: RunContext, client: GitHubClient | None = None) -> int:
    context.update_stage("search", "running")
    LOGGER.info("[search] Starting repository discovery for run %s", context.run_id)
    owns_client = client is None
    active_client = client or GitHubClient.from_environment(
        context.config.collection, logger=_api_logger(context)
    )
    try:
        for base_query in context.config.ecosystem.queries:
            _collect_base_query(context, active_client, base_query)
        with connect(context.database_path, read_only=True) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM repositories WHERE run_id = ?", [context.run_id]
            ).fetchone()[0]
        context.update_stage("search", "complete", row_count=count)
        LOGGER.info("[search] Complete: %,d unique repositories", count)
        return count
    except Exception as error:
        context.update_stage("search", "failed", message=str(error))
        raise
    finally:
        if owns_client:
            active_client.close()


def _collect_base_query(context: RunContext, client: GitHubClient, base_query: str) -> None:
    config = context.config
    stack = [(config.ecosystem.minimum_stars, config.ecosystem.maximum_stars)]
    while stack:
        if _reached_limit(context):
            return
        star_min, star_max = stack.pop()
        with connect(context.database_path, read_only=True) as connection:
            row = connection.execute(
                """
                SELECT status FROM search_slices
                WHERE run_id = ? AND base_query = ? AND star_min = ? AND star_max = ?
                """,
                [context.run_id, base_query, star_min, star_max],
            ).fetchone()
        if row and row[0] == "complete":
            continue

        query = build_search_query(base_query, star_min, star_max)
        LOGGER.info("[search] Counting slice: stars %s..%s", star_min, star_max)
        count_body = client.rest_get(
            "/search/repositories", params={"q": query, "per_page": 1}, resource="search"
        )
        if count_body is None:
            raise CollectionError(f"Search count unexpectedly returned no data: {query}")
        total = int(count_body.get("total_count", 0))
        LOGGER.info(
            "[search] Slice stars %s..%s contains %,d repositories",
            star_min,
            star_max,
            total,
        )
        if count_body.get("incomplete_results"):
            raise CollectionError(f"GitHub returned incomplete results while counting: {query}")
        _upsert_slice(context, base_query, star_min, star_max, total, 0, "counted")

        if total > config.collection.search_max_results:
            if star_min == star_max:
                _upsert_slice(context, base_query, star_min, star_max, total, 0, "unsplittable")
                raise CollectionError(
                    f"More than 1,000 repositories have exactly {star_min} stars for query: "
                    f"{base_query}. Add another qualifier to make the sample complete."
                )
            lower, upper = split_star_range(star_min, star_max)
            LOGGER.info(
                "[search] Splitting oversized slice into %s..%s and %s..%s",
                *lower,
                *upper,
            )
            stack.append(upper)
            stack.append(lower)
            continue

        fetched, complete = _fetch_slice(
            context, client, base_query, query, star_min, star_max, total
        )
        _upsert_slice(
            context,
            base_query,
            star_min,
            star_max,
            total,
            fetched,
            "complete" if complete else "partial",
        )
        if not complete and not _reached_limit(context):
            raise CollectionError(f"Search slice ended before all results were fetched: {query}")


def _fetch_slice(
    context: RunContext,
    client: GitHubClient,
    base_query: str,
    query: str,
    star_min: int,
    star_max: int,
    total: int,
) -> tuple[int, bool]:
    per_page = context.config.collection.search_per_page
    pages = math.ceil(total / per_page)
    fetched = 0
    for page in range(1, pages + 1):
        if _reached_limit(context):
            return fetched, False
        body = client.rest_get(
            "/search/repositories",
            params={
                "q": query,
                "per_page": per_page,
                "page": page,
                "sort": "stars",
                "order": "desc",
            },
            resource="search",
        )
        if body is None:
            raise CollectionError(f"Search page {page} unexpectedly returned no data: {query}")
        if body.get("incomplete_results"):
            raise CollectionError(f"GitHub returned incomplete results for page {page}: {query}")
        items = body.get("items") or []
        limit = context.config.collection.max_repositories
        if limit:
            with connect(context.database_path, read_only=True) as connection:
                current_count = connection.execute(
                    "SELECT COUNT(*) FROM repositories WHERE run_id = ?", [context.run_id]
                ).fetchone()[0]
            items = items[: max(0, limit - current_count)]
        with connect(context.database_path) as connection:
            for item in items:
                _upsert_repository(connection, context.run_id, item, base_query)
        fetched += len(items)
        LOGGER.info(
            "[search] Page %d/%d: received %d repositories; slice progress %,d/%,d",
            page,
            pages,
            len(items),
            min(fetched, total),
            total,
        )
        _upsert_slice(context, base_query, star_min, star_max, total, fetched, "fetching")
        if len(items) < per_page:
            break
    return fetched, fetched >= total


def _reached_limit(context: RunContext) -> bool:
    limit = context.config.collection.max_repositories
    if not limit:
        return False
    with connect(context.database_path, read_only=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM repositories WHERE run_id = ?", [context.run_id]
        ).fetchone()[0]
    return count >= limit


def _upsert_slice(
    context: RunContext,
    base_query: str,
    star_min: int,
    star_max: int,
    total: int,
    fetched: int,
    status: str,
) -> None:
    with connect(context.database_path) as connection:
        connection.execute(
            """
            INSERT INTO search_slices
                (run_id, base_query, star_min, star_max, total_count, fetched_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, base_query, star_min, star_max) DO UPDATE SET
                total_count = excluded.total_count,
                fetched_count = excluded.fetched_count,
                status = excluded.status,
                updated_at = now()
            """,
            [context.run_id, base_query, star_min, star_max, total, fetched, status],
        )


def _upsert_repository(connection, run_id: str, item: dict[str, Any], base_query: str) -> None:
    repository_id = int(item["id"])
    existing = connection.execute(
        "SELECT found_by_queries_json FROM repositories WHERE run_id = ? AND repository_id = ?",
        [run_id, repository_id],
    ).fetchone()
    found_by = set(json.loads(existing[0])) if existing else set()
    found_by.add(base_query)
    owner = item.get("owner") or {}
    license_data = item.get("license") or {}
    values = [
        run_id,
        repository_id,
        item.get("node_id"),
        item["full_name"],
        owner.get("login") or item["full_name"].split("/", 1)[0],
        item.get("name") or item["full_name"].split("/", 1)[1],
        item.get("description"),
        item.get("html_url"),
        item.get("language"),
        json.dumps(item.get("topics") or [], sort_keys=True),
        json.dumps(sorted(found_by)),
        int(item.get("stargazers_count") or 0),
        int(item.get("forks_count") or 0),
        int(item.get("watchers_count") or 0),
        int(item.get("open_issues_count") or 0),
        int(item.get("size") or 0),
        license_data.get("spdx_id"),
        item.get("created_at"),
        item.get("updated_at"),
        item.get("pushed_at"),
        item.get("default_branch"),
        bool(item.get("fork")),
        bool(item.get("archived")),
    ]
    connection.execute(
        """
        INSERT INTO repositories (
            run_id, repository_id, node_id, full_name, owner, name, description, html_url,
            primary_language, topics_json, found_by_queries_json, stars, forks, watchers,
            open_issues, size_kb, license, created_at, updated_at, pushed_at, default_branch,
            is_fork, is_archived
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id, repository_id) DO UPDATE SET
            node_id = excluded.node_id,
            full_name = excluded.full_name,
            owner = excluded.owner,
            name = excluded.name,
            description = excluded.description,
            html_url = excluded.html_url,
            primary_language = excluded.primary_language,
            topics_json = excluded.topics_json,
            found_by_queries_json = excluded.found_by_queries_json,
            stars = excluded.stars,
            forks = excluded.forks,
            watchers = excluded.watchers,
            open_issues = excluded.open_issues,
            size_kb = excluded.size_kb,
            license = excluded.license,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            pushed_at = excluded.pushed_at,
            default_branch = excluded.default_branch,
            is_fork = excluded.is_fork,
            is_archived = excluded.is_archived,
            collected_at = now()
        """,
        values,
    )


def build_enrichment_query(
    full_names: Iterable[str], dependency_files: Iterable[str] = ()
) -> str:
    # dependency_files remains accepted for callers from pre-0.3 releases. File contents are
    # deliberately no longer requested through GraphQL; the tree/blob retrieval layer owns them.
    del dependency_files
    parts = []
    for index, full_name in enumerate(full_names):
        owner, name = full_name.split("/", 1)
        parts.append(
            f"""  r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
    databaseId
    nameWithOwner
    defaultBranchRef {{
      target {{
        ... on Commit {{ oid tree {{ oid }} }}
      }}
    }}
    languages(first: 100, orderBy: {{field: SIZE, direction: DESC}}) {{
      edges {{ size node {{ name }} }}
    }}
  }}"""
        )
    return "query {\n  rateLimit { cost remaining resetAt }\n" + "\n".join(parts) + "\n}"


@dataclass(frozen=True)
class RepositorySnapshot:
    repository_id: int
    full_name: str
    commit_sha: str
    tree_sha: str
    languages: dict[str, int]
    source: str


def collect_enrichment(
    context: RunContext, client: GitHubClient | None = None, *, refresh: bool = False
) -> int:
    context.update_stage("enrich", "running")
    LOGGER.info("[enrich] Loading repositories that still need enrichment")
    owns_client = client is None
    active_client = client or GitHubClient.from_environment(
        context.config.collection, logger=_api_logger(context)
    )
    try:
        with connect(context.database_path, read_only=True) as connection:
            pending = connection.execute(
                """
                SELECT repository_id, full_name, default_branch, size_kb FROM repositories r
                WHERE run_id = ? AND (? OR NOT EXISTS (
                    SELECT 1 FROM repository_enrichment e
                    WHERE e.run_id = r.run_id AND e.repository_id = r.repository_id
                      AND e.status IN ('ok', 'not_found')
                ))
                ORDER BY stars DESC, repository_id
                """,
                [context.run_id, refresh],
            ).fetchall()
        strategy = _select_strategy(context, active_client, len(pending))
        LOGGER.info(
            "[enrich] Strategy: %s | pending repositories: %,d",
            strategy,
            len(pending),
        )
        batch_size = context.config.collection.graphql_batch_size
        processed = 0
        for index in range(0, len(pending), batch_size):
            chunk = pending[index : index + batch_size]
            LOGGER.info(
                "[enrich] Processing batch %d-%d of %,d",
                index + 1,
                index + len(chunk),
                len(pending),
            )
            snapshots: dict[int, RepositorySnapshot] = {}
            graphql_errors: dict[int, list[dict[str, Any]]] = {}
            if strategy == "hybrid":
                try:
                    snapshots, graphql_errors = _graphql_snapshots(active_client, chunk)
                except (CollectionError, TransientCollectionError):
                    # A GraphQL outage must not stop a resumable run. REST is slower but complete.
                    LOGGER.warning(
                        "[enrich] GraphQL batch failed; falling back to REST for this batch"
                    )
                    snapshots = {}

            for row in chunk:
                repository_id, full_name, default_branch, size_kb = row
                repository_id = int(repository_id)
                snapshot = snapshots.get(repository_id)
                if snapshot is None:
                    try:
                        snapshot = _rest_snapshot(
                            active_client,
                            repository_id,
                            str(full_name),
                            str(default_branch or "HEAD"),
                        )
                    except CollectionError as error:
                        _save_enrichment_error(
                            context,
                            repository_id,
                            str(full_name),
                            "error",
                            str(error),
                            graphql_errors.get(repository_id),
                        )
                        processed += 1
                        if processed == len(pending) or processed % 10 == 0:
                            LOGGER.info(
                                progress_message(
                                    "enrich", processed, len(pending), f"failed: {full_name}"
                                )
                            )
                        continue
                try:
                    _enrich_snapshot(context, active_client, snapshot, int(size_kb or 0))
                except CollectionError as error:
                    _save_enrichment_error(
                        context,
                        repository_id,
                        str(full_name),
                        "error",
                        str(error),
                        graphql_errors.get(repository_id),
                    )
                processed += 1
                if processed == len(pending) or processed % 10 == 0:
                    LOGGER.info(
                        progress_message("enrich", processed, len(pending), str(full_name))
                    )

        with connect(context.database_path, read_only=True) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM repository_enrichment WHERE run_id = ? AND status = 'ok'",
                [context.run_id],
            ).fetchone()[0]
        context.update_stage("enrich", "complete", row_count=count)
        LOGGER.info("[enrich] Complete: %,d repositories enriched", count)
        return count
    except Exception as error:
        context.update_stage("enrich", "failed", message=str(error))
        raise
    finally:
        if owns_client:
            active_client.close()


def _select_strategy(context: RunContext, client: GitHubClient, pending_count: int) -> str:
    configured = context.config.collection.enrichment_strategy
    if configured == "auto":
        return "hybrid" if client.authenticated and pending_count > 1 else "optimized_rest"
    if configured == "hybrid" and not client.authenticated:
        return "optimized_rest"
    return configured


def _graphql_snapshots(
    client: GitHubClient, rows: list[tuple[Any, ...]]
) -> tuple[dict[int, RepositorySnapshot], dict[int, list[dict[str, Any]]]]:
    body = client.graphql(build_enrichment_query([str(row[1]) for row in rows]))
    data = body.get("data") or {}
    errors = body.get("errors") or []
    snapshots: dict[int, RepositorySnapshot] = {}
    mapped_errors: dict[int, list[dict[str, Any]]] = {}
    for offset, row in enumerate(rows):
        repository_id, full_name = int(row[0]), str(row[1])
        alias = f"r{offset}"
        related = [error for error in errors if (error.get("path") or [None])[0] == alias]
        if related:
            mapped_errors[repository_id] = related
        node = data.get(alias)
        target = ((node or {}).get("defaultBranchRef") or {}).get("target") or {}
        tree = target.get("tree") or {}
        if not node or not target.get("oid") or not tree.get("oid"):
            continue
        if node.get("databaseId") is not None and int(node["databaseId"]) != repository_id:
            continue
        languages = {
            edge["node"]["name"]: int(edge["size"])
            for edge in (node.get("languages") or {}).get("edges", [])
        }
        snapshots[repository_id] = RepositorySnapshot(
            repository_id,
            full_name,
            str(target["oid"]),
            str(tree["oid"]),
            languages,
            "graphql",
        )
    return snapshots, mapped_errors


def _rest_snapshot(
    client: GitHubClient, repository_id: int, full_name: str, default_branch: str
) -> RepositorySnapshot:
    commit = client.rest_get(
        f"/repos/{full_name}/commits/{default_branch}", resource="core", allow_not_found=True
    )
    languages = client.rest_get(
        f"/repos/{full_name}/languages", resource="core", allow_not_found=True
    )
    tree_sha = (((commit or {}).get("commit") or {}).get("tree") or {}).get("sha")
    commit_sha = (commit or {}).get("sha")
    if not commit_sha or not tree_sha:
        raise CollectionError(f"Default-branch snapshot unavailable for {full_name}")
    return RepositorySnapshot(
        repository_id,
        full_name,
        str(commit_sha),
        str(tree_sha),
        {str(name): int(size) for name, size in (languages or {}).items()},
        "rest",
    )


def _enrich_snapshot(
    context: RunContext,
    client: GitHubClient,
    snapshot: RepositorySnapshot,
    repository_size_kb: int,
) -> None:
    started = time.perf_counter()
    with connect(context.database_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO repository_snapshots
                (run_id, repository_id, commit_sha, tree_sha, languages_json, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                context.run_id,
                snapshot.repository_id,
                snapshot.commit_sha,
                snapshot.tree_sha,
                json.dumps(snapshot.languages, sort_keys=True),
                snapshot.source,
            ],
        )
    tree = discover_tree(
        context.database_path,
        client,
        repository_id=snapshot.repository_id,
        full_name=snapshot.full_name,
        commit_sha=snapshot.commit_sha,
        tree_sha=snapshot.tree_sha,
        config=context.config.collection,
    )
    retrieval = retrieve_relevant_files(
        context.database_path,
        client,
        repository_id=snapshot.repository_id,
        full_name=snapshot.full_name,
        commit_sha=snapshot.commit_sha,
        repository_size_kb=repository_size_kb,
        entries=tree.entries,
        dependency_files=context.config.evidence.dependency_files,
        config=context.config.collection,
    )
    root_files = [[entry.path, entry.type] for entry in tree.entries]
    workflows = [
        entry.path
        for entry in tree.entries
        if entry.type == "blob" and entry.path.lower().startswith(".github/workflows/")
    ]
    dependency_names = {
        path.rsplit("/", 1)[-1].lower() for path in context.config.evidence.dependency_files
    }
    manifests: dict[str, dict[str, Any] | None] = {}
    for file in retrieval.files:
        if file.path.rsplit("/", 1)[-1].lower() in dependency_names:
            manifests[file.path] = {
                "text": file.content.decode("utf-8", errors="replace"),
                "byte_size": len(file.content),
                "oid": file.blob_sha,
                "source": file.source,
            }
    strategy = f"{snapshot.source}+{retrieval.strategy}"
    duration_ms = (time.perf_counter() - started) * 1000
    with connect(context.database_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO repository_enrichment
                (run_id, repository_id, full_name, status, commit_sha, tree_sha,
                 retrieval_strategy, languages_json, root_files_json, workflows_json,
                 manifests_json, graphql_errors_json, error_message)
            VALUES (?, ?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?, '[]', NULL)
            """,
            [
                context.run_id,
                snapshot.repository_id,
                snapshot.full_name,
                snapshot.commit_sha,
                snapshot.tree_sha,
                strategy,
                json.dumps(snapshot.languages, sort_keys=True),
                json.dumps(root_files),
                json.dumps(workflows),
                json.dumps(manifests, sort_keys=True),
            ],
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO enrichment_metrics
                (run_id, repository_id, commit_sha, strategy, tree_requests,
                 file_requests, cache_hits, bytes_transferred, archive_used,
                 relevant_files, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                context.run_id,
                snapshot.repository_id,
                snapshot.commit_sha,
                strategy,
                tree.api_requests,
                retrieval.file_requests,
                retrieval.cache_hits + int(tree.cache_hit),
                tree.bytes_transferred + retrieval.bytes_transferred,
                retrieval.archive_used,
                len(retrieval.files),
                duration_ms,
            ],
        )


def _save_enrichment_error(
    context: RunContext,
    repository_id: int,
    full_name: str,
    status: str,
    message: str,
    errors: list[dict[str, Any]] | None = None,
) -> None:
    with connect(context.database_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO repository_enrichment
                (run_id, repository_id, full_name, status, graphql_errors_json, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [context.run_id, repository_id, full_name, status, json.dumps(errors or []), message],
        )
