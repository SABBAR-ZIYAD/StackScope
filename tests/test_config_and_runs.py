import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import duckdb

from github_ecosystem_miner.config import AppConfig, EcosystemConfig, load_config
from github_ecosystem_miner.errors import ConfigurationError, RunError
from github_ecosystem_miner.runs import create_run, load_run
from github_ecosystem_miner.storage import initialize_database


class TestConfiguration(unittest.TestCase):
    def test_project_configuration_loads(self):
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / "config.toml")
        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.collection.search_max_results, 1000)
        self.assertEqual(len(config.fingerprint), 64)

    def test_stars_qualifier_is_rejected_in_base_query(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text('[ecosystem]\nqueries=["topic:webapp stars:>10"]\n')
            with self.assertRaises(ConfigurationError):
                load_config(path)


class TestRuns(unittest.TestCase):
    def test_run_is_isolated_and_configuration_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig()
            context = create_run(root, config, "fixture-run")
            self.assertTrue(context.database_path.exists())
            self.assertTrue(context.manifest_path.exists())
            loaded = load_run(root, "fixture-run", config)
            self.assertEqual(loaded.run_id, "fixture-run")
            loaded.mark_complete()
            manifest = loaded.read_manifest()
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(len(manifest["artifacts"]["database_sha256"]), 64)
            changed = replace(config, ecosystem=EcosystemConfig(queries=("topic:python",)))
            with self.assertRaises(RunError):
                load_run(root, "fixture-run", changed)

    def test_database_migrates_from_schema_one_to_three(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old.duckdb"
            with duckdb.connect(str(database)) as connection:
                connection.execute(
                    "CREATE TABLE schema_info(version INTEGER, applied_at TIMESTAMP)"
                )
                connection.execute("INSERT INTO schema_info VALUES (1, now())")
                connection.execute("CREATE TABLE repository_enrichment(dummy INTEGER)")
                connection.execute("CREATE TABLE api_calls(dummy INTEGER)")
            initialize_database(database)
            with duckdb.connect(str(database), read_only=True) as connection:
                version = connection.execute("SELECT MAX(version) FROM schema_info").fetchone()[0]
                enrichment_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info('repository_enrichment')"
                    ).fetchall()
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT table_name FROM information_schema.tables"
                    ).fetchall()
                }
            self.assertEqual(version, 3)
            self.assertIn("commit_sha", enrichment_columns)
            self.assertIn("cluster_profiles", tables)


if __name__ == "__main__":
    unittest.main()
