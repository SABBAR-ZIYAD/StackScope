from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from github_ecosystem_miner.normalization import normalize_package
from github_ecosystem_miner.parsers import PARSERS, looks_frozen_requirements
from github_ecosystem_miner.runs import RunContext
from github_ecosystem_miner.storage import connect

RESOURCE_PATTERN = re.compile(
    r"\b(awesome|tutorials?|courses?|cheat.?sheets?|roadmap|interviews?|internships?|"
    r"books?|lectures?|notes|curated|reading.?list|papers?.?list|100.?days|30.?days|"
    r"exercises?|homework|syllabus)\b",
    re.IGNORECASE,
)

TOOLING_FILES = {
    "dockerfile": "docker",
    "docker-compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
    "compose.yml": "docker-compose",
    "compose.yaml": "docker-compose",
    ".dockerignore": "docker",
    "makefile": "make",
    ".pre-commit-config.yaml": "pre-commit",
    "tox.ini": "tox",
    "noxfile.py": "nox",
    "poetry.lock": "poetry",
    "pipfile": "pipenv",
    "uv.lock": "uv",
    "pdm.lock": "pdm",
    "mkdocs.yml": "mkdocs",
    ".readthedocs.yml": "readthedocs",
    ".gitlab-ci.yml": "gitlab-ci",
    "jenkinsfile": "jenkins",
    ".travis.yml": "travis-ci",
    "azure-pipelines.yml": "azure-pipelines",
    ".circleci": "circleci",
    "dvc.yaml": "dvc",
    "vercel.json": "vercel",
    ".devcontainer": "devcontainer",
    "cmakelists.txt": "cmake",
    "snakefile": "snakemake",
}


@dataclass(frozen=True)
class Evidence:
    technology: str
    category: str
    source: str
    raw_value: str
    confidence: float
    directness: str


def is_resource_repository(name: str, description: str | None) -> bool:
    searchable_name = name.replace("-", " ").replace("_", " ")
    return bool(
        RESOURCE_PATTERN.search(searchable_name)
        or RESOURCE_PATTERN.search(description if isinstance(description, str) else "")
    )


def clean_run(context: RunContext) -> dict[str, int]:
    context.update_stage("clean", "running")
    try:
        with connect(context.database_path) as connection:
            connection.execute("DELETE FROM technology_evidence WHERE run_id = ?", [context.run_id])
            connection.execute("DELETE FROM cleaning_audit WHERE run_id = ?", [context.run_id])
            connection.execute("DELETE FROM repository_cleaning WHERE run_id = ?", [context.run_id])
            rows = connection.execute(
                """
                SELECT r.repository_id, r.name, r.description, e.languages_json,
                       e.root_files_json, e.workflows_json, e.manifests_json
                FROM repositories r
                JOIN repository_enrichment e
                  ON e.run_id = r.run_id AND e.repository_id = r.repository_id
                WHERE r.run_id = ? AND e.status = 'ok'
                ORDER BY r.repository_id
                """,
                [context.run_id],
            ).fetchall()

            kept_count = 0
            evidence_count = 0
            for row in rows:
                repository_id, name, description = int(row[0]), row[1], row[2]
                languages = json.loads(row[3] or "{}")
                root_files = json.loads(row[4] or "[]")
                workflows = json.loads(row[5] or "[]")
                manifests = json.loads(row[6] or "{}")
                evidence, audit = extract_evidence(
                    languages,
                    root_files,
                    workflows,
                    manifests,
                    minimum_language_share=context.config.evidence.minimum_language_share,
                )
                snapshot = connection.execute(
                    """
                    SELECT commit_sha FROM repository_enrichment
                    WHERE run_id = ? AND repository_id = ? AND status = 'ok'
                    """,
                    [context.run_id, repository_id],
                ).fetchone()
                for item in evidence:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO technology_evidence
                            (run_id, repository_id, technology, category, source,
                             raw_value, confidence, directness)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            context.run_id,
                            repository_id,
                            item.technology,
                            item.category,
                            item.source,
                            item.raw_value,
                            item.confidence,
                            item.directness,
                        ],
                    )
                    if snapshot and snapshot[0]:
                        connection.execute(
                            """
                            INSERT OR REPLACE INTO technology_evidence_snapshots
                                (run_id, repository_id, commit_sha, technology,
                                 category, source, confidence)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                context.run_id,
                                repository_id,
                                snapshot[0],
                                item.technology,
                                item.category,
                                item.source,
                                item.confidence,
                            ],
                        )
                for source, raw_value, normalized, action, reason in audit:
                    connection.execute(
                        """
                        INSERT INTO cleaning_audit
                            (run_id, repository_id, source, raw_value, normalized_value,
                             action, reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            context.run_id,
                            repository_id,
                            source,
                            raw_value,
                            normalized,
                            action,
                            reason,
                        ],
                    )

                package_count = len(
                    {item.technology for item in evidence if item.category == "package"}
                )
                technology_count = len({item.technology for item in evidence})
                resource = is_resource_repository(name, description)
                if context.config.cleaning.exclude_resource_repositories and resource:
                    kept, reason = False, "resource_repository"
                elif package_count < context.config.cleaning.minimum_packages:
                    kept, reason = False, "insufficient_packages"
                else:
                    kept, reason = True, "kept"
                connection.execute(
                    """
                    INSERT INTO repository_cleaning
                        (run_id, repository_id, kept, reason, package_count,
                         technology_count, is_resource)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        context.run_id,
                        repository_id,
                        kept,
                        reason,
                        package_count,
                        technology_count,
                        resource,
                    ],
                )
                kept_count += int(kept)
                evidence_count += len(evidence)

        result = {
            "repositories_enriched": len(rows),
            "repositories_kept": kept_count,
            "evidence_rows": evidence_count,
        }
        context.update_stage("clean", "complete", row_count=kept_count, message=json.dumps(result))
        return result
    except Exception as error:
        context.update_stage("clean", "failed", message=str(error))
        raise


def extract_evidence(
    languages: dict[str, int],
    root_files: list[list[str]],
    workflows: list[str],
    manifests: dict[str, dict[str, Any] | None],
    *,
    minimum_language_share: float,
) -> tuple[list[Evidence], list[tuple[str, str, str | None, str, str]]]:
    evidence: list[Evidence] = []
    audit: list[tuple[str, str, str | None, str, str]] = []

    total_bytes = sum(max(0, int(size)) for size in languages.values())
    for language, size in languages.items():
        share = int(size) / total_bytes if total_bytes else 0
        technology = f"lang:{language}"
        if share >= minimum_language_share:
            evidence.append(
                Evidence(technology, "language", "github_languages", str(size), 1.0, "observed")
            )
            audit.append(("github_languages", language, technology, "accepted", "share_threshold"))
        else:
            audit.append(
                ("github_languages", language, technology, "dropped", "below_share_threshold")
            )

    root_paths = {entry[0].lower() for entry in root_files if entry}
    for root_path in sorted(root_paths):
        root_name = PurePosixPath(root_path).name
        technology = TOOLING_FILES.get(root_name)
        if technology:
            evidence.append(
                Evidence(technology, "tooling", "root_files", root_name, 1.0, "observed")
            )
            audit.append(("root_files", root_name, technology, "accepted", "tooling_marker"))
        if root_path.endswith(".tf"):
            evidence.append(
                Evidence("terraform", "tooling", "root_files", root_name, 1.0, "observed")
            )
            audit.append(("root_files", root_name, "terraform", "accepted", "terraform_file"))
    if workflows:
        evidence.append(
            Evidence("github-actions", "tooling", "workflows", ",".join(workflows), 1.0, "observed")
        )
        audit.append(
            ("workflows", ",".join(workflows), "github-actions", "accepted", "workflow_files")
        )

    for path, blob in manifests.items():
        if not blob or not isinstance(blob.get("text"), str):
            continue
        manifest_name = PurePosixPath(path.lower()).name
        parser = PARSERS.get(manifest_name)
        if parser is None:
            audit.append((path, path, None, "unsupported", "no_parser"))
            continue
        text = blob["text"]
        frozen = manifest_name == "requirements.txt" and looks_frozen_requirements(text)
        for raw_name in parser(text):
            normalized, reason = normalize_package(raw_name)
            if normalized is None:
                audit.append((path, raw_name, None, "dropped", reason))
                continue
            action = "renamed" if normalized != raw_name else "accepted"
            directness = "environment_snapshot" if frozen else "declared"
            confidence = 0.5 if frozen else 1.0
            evidence.append(Evidence(normalized, "package", path, raw_name, confidence, directness))
            audit.append((path, raw_name, normalized, action, reason))

    unique = {(item.technology, item.category, item.source): item for item in evidence}
    return list(unique.values()), audit
