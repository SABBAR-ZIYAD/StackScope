import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from github_ecosystem_miner.config import AppConfig, EcosystemConfig, load_config
from github_ecosystem_miner.errors import ConfigurationError, RunError
from github_ecosystem_miner.runs import create_run, load_run


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
            changed = replace(config, ecosystem=EcosystemConfig(queries=("topic:python",)))
            with self.assertRaises(RunError):
                load_run(root, "fixture-run", changed)


if __name__ == "__main__":
    unittest.main()
