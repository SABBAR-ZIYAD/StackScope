from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any

from github_ecosystem_miner.errors import CollectionError, TransientCollectionError
from github_ecosystem_miner.github_client import GitHubClient
from github_ecosystem_miner.runs import RunContext
from github_ecosystem_miner.storage import connect


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
                    (run_id, api, endpoint, status, duration_ms, attempt, token_index,
                     remaining, reset_at, graphql_cost, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    context.run_id,
                    row["api"],
                    row["endpoint"],
                    row["status"],
                    row["duration_ms"],
                    row["attempt"],
                    row["token_index"],
                    row["remaining"],
                    row["reset_at"],
                    row["graphql_cost"],
                    row["error"],
                ],
            )

    return log


def collect_search(context: RunContext, client: GitHubClient | None = None) -> int:
    context.update_stage("search", "running")
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
        count_body = client.rest_get(
            "/search/repositories", params={"q": query, "per_page": 1}, resource="search"
        )
        total = int(count_body.get("total_count", 0))
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


def build_enrichment_query(full_names: Iterable[str], dependency_files: Iterable[str]) -> str:
    manifest_aliases = {f"manifest_{index}": path for index, path in enumerate(dependency_files)}
    manifest_fields = "\n".join(
        f'    {alias}: object(expression: "HEAD:{path}") {{ ... on Blob {{ text byteSize oid }} }}'
        for alias, path in manifest_aliases.items()
    )
    parts = []
    for index, full_name in enumerate(full_names):
        owner, name = full_name.split("/", 1)
        parts.append(
            f"""  r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
    databaseId
    nameWithOwner
    languages(first: 100, orderBy: {{field: SIZE, direction: DESC}}) {{
      edges {{ size node {{ name }} }}
    }}
    root: object(expression: "HEAD:") {{
      ... on Tree {{ entries {{ name type }} }}
    }}
    workflows: object(expression: "HEAD:.github/workflows") {{
      ... on Tree {{ entries {{ name type }} }}
    }}
{manifest_fields}
  }}"""
        )
    return "query {\n  rateLimit { cost remaining resetAt }\n" + "\n".join(parts) + "\n}"


def collect_enrichment(context: RunContext, client: GitHubClient | None = None) -> int:
    context.update_stage("enrich", "running")
    owns_client = client is None
    active_client = client or GitHubClient.from_environment(
        context.config.collection, logger=_api_logger(context)
    )
    try:
        with connect(context.database_path, read_only=True) as connection:
            pending = connection.execute(
                """
                SELECT repository_id, full_name FROM repositories r
                WHERE run_id = ? AND NOT EXISTS (
                    SELECT 1 FROM repository_enrichment e
                    WHERE e.run_id = r.run_id AND e.repository_id = r.repository_id
                      AND e.status IN ('ok', 'not_found')
                )
                ORDER BY stars DESC, repository_id
                """,
                [context.run_id],
            ).fetchall()
        batch_size = context.config.collection.graphql_batch_size
        index = 0
        while index < len(pending):
            chunk = pending[index : index + batch_size]
            try:
                body = active_client.graphql(
                    build_enrichment_query(
                        [row[1] for row in chunk], context.config.evidence.dependency_files
                    )
                )
            except TransientCollectionError:
                if batch_size == 1:
                    repository_id, full_name = chunk[0]
                    _save_enrichment_error(
                        context, repository_id, full_name, "error", "Repeated GraphQL failure"
                    )
                    index += 1
                else:
                    batch_size = max(1, batch_size // 2)
                continue

            data = body.get("data") or {}
            errors = body.get("errors") or []
            for offset, (repository_id, full_name) in enumerate(chunk):
                alias = f"r{offset}"
                node = data.get(alias)
                related_errors = [
                    error for error in errors if (error.get("path") or [None])[0] == alias
                ]
                _save_enrichment(
                    context,
                    int(repository_id),
                    full_name,
                    node,
                    related_errors,
                )
            index += len(chunk)
            if batch_size < context.config.collection.graphql_batch_size:
                batch_size = min(context.config.collection.graphql_batch_size, batch_size * 2)

        with connect(context.database_path, read_only=True) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM repository_enrichment WHERE run_id = ? AND status = 'ok'",
                [context.run_id],
            ).fetchone()[0]
        context.update_stage("enrich", "complete", row_count=count)
        return count
    except Exception as error:
        context.update_stage("enrich", "failed", message=str(error))
        raise
    finally:
        if owns_client:
            active_client.close()


def _save_enrichment(
    context: RunContext,
    repository_id: int,
    full_name: str,
    node: dict[str, Any] | None,
    errors: list[dict[str, Any]],
) -> None:
    if node is None:
        status = (
            "not_found" if any(error.get("type") == "NOT_FOUND" for error in errors) else "error"
        )
        _save_enrichment_error(
            context,
            repository_id,
            full_name,
            status,
            "; ".join(str(error.get("message", "GraphQL error")) for error in errors)
            or "GraphQL returned no repository",
            errors,
        )
        return
    if node.get("databaseId") is not None and int(node["databaseId"]) != repository_id:
        _save_enrichment_error(
            context,
            repository_id,
            full_name,
            "identity_mismatch",
            f"GraphQL returned repository ID {node['databaseId']} for expected ID {repository_id}",
            errors,
        )
        return

    languages = {
        edge["node"]["name"]: edge["size"]
        for edge in (node.get("languages") or {}).get("edges", [])
    }
    root_files = [
        [entry["name"], entry.get("type")] for entry in (node.get("root") or {}).get("entries", [])
    ]
    workflows = [entry["name"] for entry in (node.get("workflows") or {}).get("entries", [])]
    manifests: dict[str, dict[str, Any] | None] = {}
    for index, path in enumerate(context.config.evidence.dependency_files):
        blob = node.get(f"manifest_{index}")
        manifests[path] = (
            {"text": blob.get("text"), "byte_size": blob.get("byteSize"), "oid": blob.get("oid")}
            if blob
            else None
        )
    with connect(context.database_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO repository_enrichment
                (run_id, repository_id, full_name, status, languages_json, root_files_json,
                 workflows_json, manifests_json, graphql_errors_json, error_message)
            VALUES (?, ?, ?, 'ok', ?, ?, ?, ?, ?, NULL)
            """,
            [
                context.run_id,
                repository_id,
                full_name,
                json.dumps(languages, sort_keys=True),
                json.dumps(root_files),
                json.dumps(workflows),
                json.dumps(manifests, sort_keys=True),
                json.dumps(errors),
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
