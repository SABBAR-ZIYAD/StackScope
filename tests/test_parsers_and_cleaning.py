import unittest

from github_ecosystem_miner.cleaning import extract_evidence, is_resource_repository
from github_ecosystem_miner.normalization import normalize_package
from github_ecosystem_miner.parsers import (
    looks_frozen_requirements,
    parse_environment_yml,
    parse_pyproject,
    parse_requirements,
    parse_setup_cfg,
    parse_setup_py,
)


class TestDependencyParsers(unittest.TestCase):
    def test_requirements_options_comments_urls_and_extras(self):
        text = """# comment
torch>=2
scikit_learn==1.4 # inline
-r dev.txt
transformers[torch] @ https://example.test/package.whl
-e git+https://github.com/x/y.git#egg=my-lib
"""
        self.assertEqual(
            parse_requirements(text),
            ["torch", "scikit_learn", "transformers", "my-lib"],
        )

    def test_pyproject_pep621_poetry_and_groups(self):
        text = """
[project]
dependencies = ["fastapi>=0.1"]
[project.optional-dependencies]
dev = ["pytest"]
[dependency-groups]
docs = ["sphinx"]
[tool.poetry.dependencies]
python = "^3.11"
pandas = "*"
"""
        self.assertEqual(parse_pyproject(text), ["fastapi", "pytest", "sphinx", "python", "pandas"])

    def test_setup_formats(self):
        setup_py = 'setup(install_requires=["numpy", "scipy"], extras_require={"dev": ["black"]})'
        self.assertEqual(parse_setup_py(setup_py), ["numpy", "scipy", "black"])
        setup_cfg = "[options]\ninstall_requires =\n  requests\n  tqdm>=4\n"
        self.assertEqual(parse_setup_cfg(setup_cfg), ["requests", "tqdm"])

    def test_environment_ignores_channels(self):
        text = """name: demo
channels:
  - conda-forge
dependencies:
  - python=3.11
  - pytorch::pytorch=2
  - pip:
    - wandb==0.16
"""
        self.assertEqual(parse_environment_yml(text), ["python", "pytorch", "wandb"])

    def test_frozen_detection(self):
        text = "\n".join(f"package-{index}==1.0" for index in range(30))
        self.assertTrue(looks_frozen_requirements(text))


class TestCleaning(unittest.TestCase):
    def test_normalization_and_drops(self):
        self.assertEqual(normalize_package("Scikit_Learn")[0], "scikit-learn")
        self.assertEqual(normalize_package("pytorch")[0], "torch")
        self.assertIsNone(normalize_package("urllib3")[0])

    def test_evidence_has_sources_and_language_threshold(self):
        evidence, audit = extract_evidence(
            {"Python": 900, "Shell": 30, "C++": 70},
            [["Dockerfile", "blob"], ["pyproject.toml", "blob"]],
            ["ci.yml"],
            {"pyproject.toml": {"text": '[project]\ndependencies=["fastapi", "uvicorn"]'}},
            minimum_language_share=0.05,
        )
        technologies = {item.technology for item in evidence}
        self.assertIn("lang:Python", technologies)
        self.assertIn("lang:C++", technologies)
        self.assertNotIn("lang:Shell", technologies)
        self.assertIn("docker", technologies)
        self.assertIn("github-actions", technologies)
        self.assertIn("fastapi", technologies)
        self.assertTrue(audit)

    def test_resource_detection(self):
        self.assertTrue(is_resource_repository("awesome-python", None))
        self.assertFalse(is_resource_repository("fastapi", "Web framework"))


if __name__ == "__main__":
    unittest.main()
