import tempfile
import unittest
from pathlib import Path

from github_ecosystem_miner.collection import _upsert_repository, collect_enrichment, collect_search
from github_ecosystem_miner.config import AppConfig, CollectionConfig, EcosystemConfig
from github_ecosystem_miner.errors import TransientCollectionError
from github_ecosystem_miner.runs import create_run
from github_ecosystem_miner.storage import connect


def repository(repository_id):
    return {
        "id": repository_id,
        "node_id": f"node-{repository_id}",
        "full_name": f"owner/repo-{repository_id}",
        "owner": {"login": "owner"},
        "name": f"repo-{repository_id}",
        "description": "application",
        "html_url": f"https://github.com/owner/repo-{repository_id}",
        "language": "Python",
        "topics": [],
        "stargazers_count": 100 + repository_id,
        "forks_count": 0,
        "watchers_count": 0,
        "open_issues_count": 0,
        "size": 10,
        "license": None,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2025-01-01T00:00:00Z",
        "default_branch": "main",
        "fork": False,
        "archived": False,
    }


class ResilientClient:
    authenticated = True

    def __init__(self, repository_ids, *, fail_graphql=False, interrupt_at=None):
        self.repository_ids = repository_ids
        self.fail_graphql = fail_graphql
        self.interrupt_at = interrupt_at
        self.tree_calls = 0

    def graphql(self, query):
        if self.fail_graphql:
            raise TransientCollectionError("temporary GraphQL failure")
        data = {"rateLimit": {"cost": 1, "remaining": 1000}}
        for index, repository_id in enumerate(self.repository_ids):
            data[f"r{index}"] = {
                "databaseId": repository_id,
                "nameWithOwner": f"owner/repo-{repository_id}",
                "defaultBranchRef": {
                    "target": {
                        "oid": f"commit-{repository_id}",
                        "tree": {"oid": f"tree-{repository_id}"},
                    }
                },
                "languages": {"edges": [{"size": 10, "node": {"name": "Python"}}]},
            }
        return {"data": data}

    def rest_get(
        self, endpoint, *, params=None, resource="core", allow_not_found=False
    ):
        if "/commits/" in endpoint:
            repository_id = int(endpoint.split("/")[3].split("-")[-1])
            return {
                "sha": f"commit-{repository_id}",
                "commit": {"tree": {"sha": f"tree-{repository_id}"}},
            }
        if endpoint.endswith("/languages"):
            return {"Python": 10}
        if "/git/trees/" in endpoint:
            self.tree_calls += 1
            if self.interrupt_at == self.tree_calls:
                raise RuntimeError("simulated interruption")
            return {
                "truncated": False,
                "tree": [
                    {"path": "requirements.txt", "type": "blob", "sha": "blob", "size": 7}
                ],
            }
        raise AssertionError(endpoint)

    def get_bytes(self, url, **kwargs):
        return b"fastapi"


class PaginationClient:
    def __init__(self):
        self.calls = 0

    def rest_get(self, endpoint, *, params=None, resource="core", allow_not_found=False):
        self.calls += 1
        if params["per_page"] == 1:
            return {"total_count": 3, "incomplete_results": False}
        ids = [1, 2] if params["page"] == 1 else [2, 3]
        return {
            "total_count": 3,
            "incomplete_results": False,
            "items": [repository(repository_id) for repository_id in ids],
        }


class TestCollectionResilience(unittest.TestCase):
    def _context(self, directory, count):
        config = AppConfig(
            collection=CollectionConfig(
                enrichment_strategy="hybrid", archive_mode="never", max_retries=1
            )
        )
        context = create_run(Path(directory), config, "resilience")
        with connect(context.database_path) as connection:
            for repository_id in range(1, count + 1):
                _upsert_repository(connection, context.run_id, repository(repository_id), "query")
        return context

    def test_graphql_failure_falls_back_to_optimized_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self._context(directory, 1)
            client = ResilientClient([1], fail_graphql=True)
            self.assertEqual(collect_enrichment(context, client), 1)
            with connect(context.database_path, read_only=True) as connection:
                strategy = connection.execute(
                    "SELECT retrieval_strategy FROM repository_enrichment"
                ).fetchone()[0]
            self.assertTrue(strategy.startswith("rest+"))

    def test_search_paginates_deduplicates_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            context = create_run(
                Path(directory),
                AppConfig(
                    ecosystem=EcosystemConfig(
                        queries=("topic:test",), minimum_stars=0, maximum_stars=0
                    ),
                    collection=CollectionConfig(search_per_page=2),
                ),
                "pagination",
            )
            client = PaginationClient()
            self.assertEqual(collect_search(context, client), 3)
            calls_after_first_run = client.calls
            self.assertEqual(collect_search(context, client), 3)
            self.assertEqual(client.calls, calls_after_first_run)

    def test_interrupted_collection_resumes_only_pending_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self._context(directory, 2)
            first = ResilientClient([2, 1], interrupt_at=2)
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                collect_enrichment(context, first)
            with connect(context.database_path, read_only=True) as connection:
                complete = connection.execute(
                    "SELECT COUNT(*) FROM repository_enrichment WHERE status = 'ok'"
                ).fetchone()[0]
            self.assertEqual(complete, 1)

            resumed = ResilientClient([1])
            self.assertEqual(collect_enrichment(context, resumed), 2)
            self.assertEqual(resumed.tree_calls, 1)

    def test_incremental_refresh_reuses_tree_and_file_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self._context(directory, 1)
            first = ResilientClient([1])
            self.assertEqual(collect_enrichment(context, first), 1)
            refreshed = ResilientClient([1])
            self.assertEqual(collect_enrichment(context, refreshed, refresh=True), 1)
            self.assertEqual(refreshed.tree_calls, 0)


if __name__ == "__main__":
    unittest.main()
