import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from github_ecosystem_miner.association import mine_association_rules
from github_ecosystem_miner.config import AppConfig, MiningConfig
from github_ecosystem_miner.runs import create_run
from github_ecosystem_miner.storage import connect


class TestAssociationMining(unittest.TestCase):
    def test_rules_are_mined_and_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            mining = MiningConfig(
                minimum_item_support=0.20,
                minimum_support=0.30,
                minimum_confidence=0.50,
                minimum_lift=1.10,
                maximum_itemset_length=3,
                redundancy_improvement=0.05,
                community_minimum_lift=1.10,
                random_seed=42,
            )
            config = replace(AppConfig(), mining=mining)
            context = create_run(Path(directory), config, "rules-fixture")
            with connect(context.database_path) as connection:
                for repository_id in range(1, 7):
                    connection.execute(
                        "INSERT INTO repository_cleaning VALUES (?, ?, TRUE, 'kept', 2, 2, FALSE)",
                        [context.run_id, repository_id],
                    )
                for repository_id in range(1, 4):
                    for technology in ("alpha", "beta"):
                        connection.execute(
                            """
                            INSERT INTO technology_evidence
                                (run_id, repository_id, technology, category, source,
                                 raw_value, confidence, directness)
                            VALUES (?, ?, ?, 'package', 'fixture', ?, 1.0, 'declared')
                            """,
                            [context.run_id, repository_id, technology, technology],
                        )
                for repository_id in range(4, 7):
                    for technology in ("gamma", "delta"):
                        connection.execute(
                            """
                            INSERT INTO technology_evidence
                                (run_id, repository_id, technology, category, source,
                                 raw_value, confidence, directness)
                            VALUES (?, ?, ?, 'package', 'fixture', ?, 1.0, 'declared')
                            """,
                            [context.run_id, repository_id, technology, technology],
                        )
            result = mine_association_rules(context)
            self.assertGreaterEqual(result["rules"], 4)
            with connect(context.database_path, read_only=True) as connection:
                saved = connection.execute(
                    "SELECT COUNT(*) FROM association_rules WHERE run_id = ?", [context.run_id]
                ).fetchone()[0]
            self.assertEqual(saved, result["rules"])


if __name__ == "__main__":
    unittest.main()
