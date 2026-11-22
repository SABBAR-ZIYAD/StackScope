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
class AppConfig:
    schema_version: int = 1
    ecosystem: EcosystemConfig = field(default_factory=EcosystemConfig)
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    cleaning: CleaningConfig = field(default_factory=CleaningConfig)
    mining: MiningConfig = field(default_factory=MiningConfig)

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
