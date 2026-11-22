import tempfile
import unittest
from pathlib import Path

from github_ecosystem_miner.association import mine_association_rules
from github_ecosystem_miner.cleaning import clean_run
from github_ecosystem_miner.collection import collect_enrichment, collect_search
from github_ecosystem_miner.config import (
    AppConfig,
    CleaningConfig,
    CollectionConfig,
    EcosystemConfig,
    MiningConfig,
)
from github_ecosystem_miner.runs import create_run
from github_ecosystem_miner.storage import connect


class FakeGitHubClient:
    def rest_get(self, endpoint, *, params=None, resource="core"):
        self.last_endpoint = endpoint
        if params["per_page"] == 1:
            return {"total_count": 2, "incomplete_results": False, "items": []}
        return {
            "total_count": 2,
            "incomplete_results": False,
            "items": [self._repository(1), self._repository(2)],
        }

    def graphql(self, query):
        self.last_query = query
        nodes = {}
        for index in range(2):
            nodes[f"r{index}"] = {
                # Enrichment orders pending repositories by stars descending.
                "databaseId": 2 - index,
                "nameWithOwner": f"owner/repo-{2 - index}",
                "languages": {
                    "edges": [
                        {"size": 900, "node": {"name": "Python"}},
                        {"size": 100, "node": {"name": "HTML"}},
                    ]
                },
                "root": {
                    "entries": [
                        {"name": "Dockerfile", "type": "blob"},
                        {"name": "pyproject.toml", "type": "blob"},
                    ]
                },
                "workflows": {"entries": [{"name": "ci.yml", "type": "blob"}]},
                "manifest_0": None,
                "manifest_1": {
                    "text": '[project]\ndependencies=["fastapi", "uvicorn"]',
                    "byteSize": 55,
                    "oid": f"oid-{index}",
                },
                "manifest_2": None,
                "manifest_3": None,
                "manifest_4": None,
            }
        nodes["rateLimit"] = {"cost": 1, "remaining": 4999, "resetAt": "2030-01-01T00:00:00Z"}
        return {"data": nodes}

    @staticmethod
    def _repository(repository_id):
        return {
            "id": repository_id,
            "node_id": f"node-{repository_id}",
            "full_name": f"owner/repo-{repository_id}",
            "owner": {"login": "owner"},
            "name": f"repo-{repository_id}",
            "description": "A web application",
            "html_url": f"https://github.com/owner/repo-{repository_id}",
            "language": "Python",
            "topics": ["webapp"],
            "stargazers_count": 100 + repository_id,
            "forks_count": 1,
            "watchers_count": 2,
            "open_issues_count": 0,
            "size": 10,
            "license": {"spdx_id": "MIT"},
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "pushed_at": "2026-01-01T00:00:00Z",
            "default_branch": "main",
            "fork": False,
            "archived": False,
        }


class TestPipelineIntegration(unittest.TestCase):
    def test_mocked_collection_to_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                ecosystem=EcosystemConfig(
                    queries=("topic:webapp fork:false",), minimum_stars=0, maximum_stars=100
                ),
                collection=CollectionConfig(max_repositories=0),
                cleaning=CleaningConfig(minimum_packages=2),
                mining=MiningConfig(
                    minimum_item_support=0.50,
                    minimum_support=0.50,
                    minimum_confidence=0.50,
                    minimum_lift=0.50,
                    maximum_itemset_length=3,
                    redundancy_improvement=0.05,
                    community_minimum_lift=0.50,
                    random_seed=42,
                ),
            )
            context = create_run(Path(directory), config, "integration")
            client = FakeGitHubClient()

            self.assertEqual(collect_search(context, client), 2)
            self.assertEqual(collect_enrichment(context, client), 2)
            cleaning = clean_run(context)
            self.assertEqual(cleaning["repositories_kept"], 2)
            self.assertGreaterEqual(cleaning["evidence_rows"], 12)
            mining = mine_association_rules(context)
            self.assertGreater(mining["rules"], 0)

            with connect(context.database_path, read_only=True) as connection:
                audit_count = connection.execute(
                    "SELECT COUNT(*) FROM cleaning_audit WHERE run_id = ?", [context.run_id]
                ).fetchone()[0]
                completed_stages = connection.execute(
                    "SELECT COUNT(*) FROM stage_status WHERE run_id = ? AND status = 'complete'",
                    [context.run_id],
                ).fetchone()[0]
            self.assertGreater(audit_count, 0)
            self.assertEqual(completed_stages, 4)


if __name__ == "__main__":
    unittest.main()
