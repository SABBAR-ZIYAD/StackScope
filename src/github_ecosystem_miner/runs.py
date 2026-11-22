from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from github_ecosystem_miner.config import AppConfig
from github_ecosystem_miner.errors import RunError
from github_ecosystem_miner.storage import connect, initialize_database

RUN_NAME_PATTERN = re.compile(r"[^a-z0-9]+")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _run_slug(config: AppConfig, requested_name: str | None) -> str:
    source = requested_name or config.ecosystem.queries[0]
    topic = re.search(r"\btopic:([^\s]+)", source, re.IGNORECASE)
    language = re.search(r"\blanguage:([^\s]+)", source, re.IGNORECASE)
    if requested_name is None:
        if topic:
            source = f"{topic.group(1)}-ecosystem"
        elif language:
            source = f"{language.group(1)}-ecosystem"
        else:
            source = "github-ecosystem"
    slug = RUN_NAME_PATTERN.sub("-", source.lower()).strip("-")
    return (slug or "github-ecosystem")[:48].rstrip("-")


def default_run_id(config: AppConfig, run_name: str | None = None) -> str:
    local_timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{local_timestamp}_{_run_slug(config, run_name)}_{config.fingerprint[:8]}"


def _git_commit(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _dependency_versions() -> dict[str, str]:
    packages = (
        "duckdb",
        "httpx",
        "mlxtend",
        "networkx",
        "pandas",
        "scikit-learn",
        "scipy",
    )
    versions = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


@dataclass(frozen=True)
class RunContext:
    project_root: Path
    run_id: str
    run_dir: Path
    database_path: Path
    manifest_path: Path
    config: AppConfig

    def update_stage(
        self,
        stage: str,
        status: str,
        *,
        row_count: int | None = None,
        message: str | None = None,
    ) -> None:
        manifest = self.read_manifest()
        stages = manifest.setdefault("stages", {})
        entry = stages.setdefault(stage, {})
        if status == "running" and "started_at" not in entry:
            entry["started_at"] = utc_now()
        if status in {"complete", "failed"}:
            entry["completed_at"] = utc_now()
        entry["status"] = status
        entry["row_count"] = row_count
        entry["message"] = message
        manifest["updated_at"] = utc_now()
        manifest["status"] = "failed" if status == "failed" else "active"
        self.write_manifest(manifest)

        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO stage_status
                    (run_id, stage, status, started_at, completed_at, row_count, message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self.run_id,
                    stage,
                    status,
                    entry.get("started_at"),
                    entry.get("completed_at"),
                    row_count,
                    message,
                ],
            )
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                [manifest["status"], manifest["updated_at"], self.run_id],
            )

    def read_manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.manifest_path)

    def mark_complete(self) -> None:
        completed_at = utc_now()
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE runs SET status = 'complete', updated_at = ? WHERE run_id = ?",
                [completed_at, self.run_id],
            )
        digest = hashlib.sha256()
        with self.database_path.open("rb") as database:
            for chunk in iter(lambda: database.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest = self.read_manifest()
        manifest["status"] = "complete"
        manifest["completed_at"] = completed_at
        manifest["updated_at"] = completed_at
        manifest.setdefault("artifacts", {})["database_sha256"] = digest.hexdigest()
        self.write_manifest(manifest)


def create_run(
    project_root: str | Path,
    config: AppConfig,
    run_id: str | None = None,
    run_name: str | None = None,
) -> RunContext:
    root = Path(project_root).resolve()
    base_id = run_id or default_run_id(config, run_name)
    run_dir = root / "data" / "runs" / base_id
    if run_dir.exists():
        raise RunError(f"Run already exists: {base_id}")
    run_dir.mkdir(parents=True)
    database_path = run_dir / "ecosystem.duckdb"
    manifest_path = run_dir / "manifest.json"
    initialize_database(database_path)

    created_at = utc_now()
    manifest = {
        "schema_version": 1,
        "run_id": base_id,
        "run_name": _run_slug(config, run_name),
        "status": "created",
        "created_at": created_at,
        "updated_at": created_at,
        "config_hash": config.fingerprint,
        "config": config.to_dict(),
        "git_commit": _git_commit(root),
        "python_version": platform.python_version(),
        "dependency_versions": _dependency_versions(),
        "github_api_version": "2022-11-28",
        "random_seeds": {
            "mining": config.mining.random_seed,
            "clustering": config.clustering.random_seed,
            "robustness": config.robustness.random_seed,
        },
        "artifacts": {"database": "ecosystem.duckdb"},
        "stages": {},
    }
    context = RunContext(root, base_id, run_dir, database_path, manifest_path, config)
    context.write_manifest(manifest)
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO runs
                (run_id, config_hash, config_json, status, created_at, updated_at,
                 git_commit, python_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                base_id,
                config.fingerprint,
                config.canonical_json(),
                "created",
                created_at,
                created_at,
                manifest["git_commit"],
                manifest["python_version"],
            ],
        )
    return context


def load_run(project_root: str | Path, run_id: str, config: AppConfig) -> RunContext:
    root = Path(project_root).resolve()
    run_dir = root / "data" / "runs" / run_id
    manifest_path = run_dir / "manifest.json"
    database_path = run_dir / "ecosystem.duckdb"
    if not manifest_path.exists() or not database_path.exists():
        raise RunError(f"Run does not exist or is incomplete: {run_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("config_hash") != config.fingerprint:
        raise RunError(
            "Configuration does not match this run. Use the original configuration "
            "or create a new run."
        )
    initialize_database(database_path)
    return RunContext(root, run_id, run_dir, database_path, manifest_path, config)


def list_runs(project_root: str | Path) -> list[dict[str, Any]]:
    runs_dir = Path(project_root).resolve() / "data" / "runs"
    if not runs_dir.exists():
        return []
    manifests = []
    for path in sorted(runs_dir.glob("*/manifest.json"), reverse=True):
        try:
            manifests.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            manifests.append({"run_id": path.parent.name, "status": "invalid_manifest"})
    return manifests
