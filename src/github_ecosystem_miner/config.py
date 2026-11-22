from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from github_ecosystem_miner.errors import ConfigurationError


@dataclass(frozen=True)
class EcosystemConfig:
    queries: tuple[str, ...] = ("topic:webapp fork:false archived:false",)
    minimum_stars: int = 0
    maximum_stars: int = 1_000_000


@dataclass(frozen=True)
class CollectionConfig:
    search_per_page: int = 100
    search_max_results: int = 1000
    search_interval_seconds: float = 2.1
    graphql_batch_size: int = 20
    max_repositories: int = 0
    request_timeout_seconds: float = 60.0
    max_retries: int = 6
    wait_for_rate_limit: bool = True
    enrichment_strategy: str = "auto"
    tree_recursive: bool = True
    tree_fallback_max_calls: int = 50
    tree_fallback_max_depth: int = 8
    raw_file_max_bytes: int = 1_000_000
    archive_mode: str = "never"
    archive_file_threshold: int = 12
    archive_max_repository_kb: int = 50_000
    archive_max_download_bytes: int = 50_000_000
    archive_max_extracted_bytes: int = 200_000_000
    archive_max_files: int = 50_000
    core_rate_reserve: int = 50
    search_rate_reserve: int = 2
    graphql_rate_reserve: int = 50
    benchmark_repository_limit: int = 3


@dataclass(frozen=True)
class EvidenceConfig:
    minimum_language_share: float = 0.05
    dependency_files: tuple[str, ...] = (
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "environment.yml",
    )


@dataclass(frozen=True)
class CleaningConfig:
    minimum_packages: int = 2
    exclude_resource_repositories: bool = True


@dataclass(frozen=True)
class MiningConfig:
    minimum_item_support: float = 0.01
    minimum_support: float = 0.02
    minimum_confidence: float = 0.50
    minimum_lift: float = 1.50
    maximum_itemset_length: int = 4
    redundancy_improvement: float = 0.05
    community_minimum_lift: float = 2.0
    random_seed: int = 42
    support_grid: tuple[float, ...] = (0.05, 0.03, 0.02, 0.015, 0.01)


@dataclass(frozen=True)
class ClusteringConfig:
    minimum_clusters: int = 2
    maximum_clusters: int = 8
    svd_components: int = 20
    owner_repository_cap: int = 5
    minimum_technologies: int = 2
    random_seed: int = 42
    stability_seeds: tuple[int, ...] = (11, 23, 42, 71, 101)
    representative_count: int = 5


@dataclass(frozen=True)
class RobustnessConfig:
    bootstrap_iterations: int = 50
    stable_rule_frequency: float = 0.70
    star_quantile: float = 0.50
    random_seed: int = 42


@dataclass(frozen=True)
class AppConfig:
    schema_version: int = 1
    ecosystem: EcosystemConfig = field(default_factory=EcosystemConfig)
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    cleaning: CleaningConfig = field(default_factory=CleaningConfig)
    mining: MiningConfig = field(default_factory=MiningConfig)
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    robustness: RobustnessConfig = field(default_factory=RobustnessConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"Configuration section [{name}] must be a table")
    return value


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigurationError(f"Configuration file does not exist: {config_path}")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {config_path}: {error}") from error

    try:
        ecosystem_data = _section(data, "ecosystem")
        evidence_data = _section(data, "evidence")
        ecosystem = EcosystemConfig(
            **{
                **ecosystem_data,
                "queries": tuple(ecosystem_data.get("queries", EcosystemConfig.queries)),
            }
        )
        evidence = EvidenceConfig(
            **{
                **evidence_data,
                "dependency_files": tuple(
                    evidence_data.get("dependency_files", EvidenceConfig.dependency_files)
                ),
            }
        )
        mining_data = _section(data, "mining")
        clustering_data = _section(data, "clustering")
        config = AppConfig(
            schema_version=int(data.get("schema_version", 1)),
            ecosystem=ecosystem,
            collection=CollectionConfig(**_section(data, "collection")),
            evidence=evidence,
            cleaning=CleaningConfig(**_section(data, "cleaning")),
            mining=MiningConfig(
                **{
                    **mining_data,
                    "support_grid": tuple(
                        mining_data.get("support_grid", MiningConfig.support_grid)
                    ),
                }
            ),
            clustering=ClusteringConfig(
                **{
                    **clustering_data,
                    "stability_seeds": tuple(
                        clustering_data.get(
                            "stability_seeds", ClusteringConfig.stability_seeds
                        )
                    ),
                }
            ),
            robustness=RobustnessConfig(**_section(data, "robustness")),
        )
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"Invalid configuration value: {error}") from error

    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    if config.schema_version != 1:
        raise ConfigurationError(f"Unsupported configuration schema: {config.schema_version}")
    if not config.ecosystem.queries or any(not query.strip() for query in config.ecosystem.queries):
        raise ConfigurationError("ecosystem.queries must contain at least one non-empty query")
    if any("stars:" in query.lower() for query in config.ecosystem.queries):
        raise ConfigurationError(
            "Do not put stars: in ecosystem queries; use minimum_stars/maximum_stars"
        )
    if config.ecosystem.minimum_stars < 0:
        raise ConfigurationError("ecosystem.minimum_stars must not be negative")
    if config.ecosystem.maximum_stars < config.ecosystem.minimum_stars:
        raise ConfigurationError("ecosystem.maximum_stars must be >= minimum_stars")
    if not 1 <= config.collection.search_per_page <= 100:
        raise ConfigurationError("collection.search_per_page must be between 1 and 100")
    if config.collection.search_max_results != 1000:
        raise ConfigurationError("collection.search_max_results must be 1000 for GitHub Search")
    if not 1 <= config.collection.graphql_batch_size <= 50:
        raise ConfigurationError("collection.graphql_batch_size must be between 1 and 50")
    if config.collection.max_repositories < 0:
        raise ConfigurationError("collection.max_repositories must not be negative")
    if config.collection.enrichment_strategy not in {"auto", "optimized_rest", "hybrid"}:
        raise ConfigurationError(
            "collection.enrichment_strategy must be auto, optimized_rest, or hybrid"
        )
    if config.collection.archive_mode not in {"never", "auto", "always"}:
        raise ConfigurationError("collection.archive_mode must be never, auto, or always")
    for name in (
        "tree_fallback_max_calls",
        "tree_fallback_max_depth",
        "raw_file_max_bytes",
        "archive_file_threshold",
        "archive_max_repository_kb",
        "archive_max_download_bytes",
        "archive_max_extracted_bytes",
        "archive_max_files",
        "benchmark_repository_limit",
    ):
        if getattr(config.collection, name) < 1:
            raise ConfigurationError(f"collection.{name} must be at least 1")
    for name in ("core_rate_reserve", "search_rate_reserve", "graphql_rate_reserve"):
        if getattr(config.collection, name) < 0:
            raise ConfigurationError(f"collection.{name} must not be negative")
    for name in ("minimum_language_share",):
        value = getattr(config.evidence, name)
        if not 0 <= value <= 1:
            raise ConfigurationError(f"evidence.{name} must be between 0 and 1")
    for name in ("minimum_item_support", "minimum_support", "minimum_confidence"):
        value = getattr(config.mining, name)
        if not 0 < value <= 1:
            raise ConfigurationError(f"mining.{name} must be in (0, 1]")
    if config.mining.minimum_lift <= 0 or config.mining.community_minimum_lift <= 0:
        raise ConfigurationError("Mining lift thresholds must be positive")
    if not 2 <= config.mining.maximum_itemset_length <= 6:
        raise ConfigurationError("mining.maximum_itemset_length must be between 2 and 6")
    if not config.mining.support_grid or any(
        not 0 < threshold <= 1 for threshold in config.mining.support_grid
    ):
        raise ConfigurationError("mining.support_grid values must be in (0, 1]")
    if config.clustering.minimum_clusters < 2:
        raise ConfigurationError("clustering.minimum_clusters must be at least 2")
    if config.clustering.maximum_clusters < config.clustering.minimum_clusters:
        raise ConfigurationError(
            "clustering.maximum_clusters must be >= clustering.minimum_clusters"
        )
    if config.clustering.svd_components < 2:
        raise ConfigurationError("clustering.svd_components must be at least 2")
    if config.clustering.owner_repository_cap < 1:
        raise ConfigurationError("clustering.owner_repository_cap must be at least 1")
    if not config.clustering.stability_seeds:
        raise ConfigurationError("clustering.stability_seeds must not be empty")
    if config.robustness.bootstrap_iterations < 1:
        raise ConfigurationError("robustness.bootstrap_iterations must be at least 1")
    if not 0 < config.robustness.stable_rule_frequency <= 1:
        raise ConfigurationError("robustness.stable_rule_frequency must be in (0, 1]")
    if not 0 < config.robustness.star_quantile < 1:
        raise ConfigurationError("robustness.star_quantile must be in (0, 1)")
