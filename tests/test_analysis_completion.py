import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from github_ecosystem_miner.association import mine_association_rules
from github_ecosystem_miner.clustering import cluster_repositories
from github_ecosystem_miner.collection import _upsert_repository
from github_ecosystem_miner.config import (
    AppConfig,
    ClusteringConfig,
    MiningConfig,
    RobustnessConfig,
)
from github_ecosystem_miner.reporting import export_run, generate_report
from github_ecosystem_miner.robustness import analyze_robustness
from github_ecosystem_miner.runs import create_run
from github_ecosystem_miner.storage import connect


def repository(repository_id):
    created_year = 2018 if repository_id <= 6 else 2024
    return {
        "id": repository_id,
        "node_id": f"node-{repository_id}",
        "full_name": f"owner-{repository_id}/repo-{repository_id}",
        "owner": {"login": f"owner-{repository_id}"},
        "name": f"repo-{repository_id}",
        "description": "fixture application",
        "html_url": f"https://github.com/owner-{repository_id}/repo-{repository_id}",
        "language": "Python",
        "topics": ["fixture"],
        "stargazers_count": repository_id * 10,
        "forks_count": repository_id,
        "watchers_count": repository_id,
        "open_issues_count": 0,
        "size": 20,
        "license": {"spdx_id": "MIT"},
        "created_at": f"{created_year}-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-01-01T00:00:00Z",
        "default_branch": "main",
        "fork": False,
        "archived": False,
    }


class TestCompletedAnalysis(unittest.TestCase):
    def test_cluster_robustness_report_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                AppConfig(),
                mining=MiningConfig(
                    minimum_item_support=0.1,
                    minimum_support=0.25,
                    minimum_confidence=0.5,
                    minimum_lift=1.1,
                    maximum_itemset_length=3,
                    community_minimum_lift=1.1,
                    support_grid=(0.5, 0.25),
                ),
                clustering=ClusteringConfig(
                    minimum_clusters=2,
                    maximum_clusters=3,
                    svd_components=3,
                    owner_repository_cap=2,
                    stability_seeds=(11, 42),
                    representative_count=2,
                ),
                robustness=RobustnessConfig(bootstrap_iterations=5),
            )
            context = create_run(Path(directory), config, "completed-analysis")
            with connect(context.database_path) as connection:
                for repository_id in range(1, 13):
                    _upsert_repository(
                        connection,
                        context.run_id,
                        repository(repository_id),
                        "fixture",
                    )
                    connection.execute(
                        "INSERT INTO repository_cleaning VALUES (?, ?, TRUE, 'kept', 3, 3, FALSE)",
                        [context.run_id, repository_id],
                    )
                    technologies = (
                        ("alpha", "beta", "lang:Python")
                        if repository_id <= 6
                        else ("gamma", "delta", "lang:JavaScript")
                    )
                    for technology in technologies:
                        category = "language" if technology.startswith("lang:") else "package"
                        connection.execute(
                            """
                            INSERT INTO technology_evidence
                                (run_id, repository_id, technology, category, source,
                                 raw_value, confidence, directness)
                            VALUES (?, ?, ?, ?, 'fixture', ?, 1.0, 'declared')
                            """,
                            [context.run_id, repository_id, technology, category, technology],
                        )

            mine_association_rules(context)
            clustered = cluster_repositories(context)
            self.assertEqual(clustered["clusters"], 2)
            robustness = analyze_robustness(context)
            self.assertEqual(robustness["repositories"], 12)
            report = generate_report(context)
            self.assertTrue(Path(report["path"]).exists())
            self.assertIn("StackScope", Path(report["path"]).read_text(encoding="utf-8"))
            exported = export_run(context, "parquet")
            self.assertGreater(len(exported["tables"]), 5)
            self.assertTrue(all(Path(item["path"]).exists() for item in exported["tables"]))


if __name__ == "__main__":
    unittest.main()
