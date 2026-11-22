from __future__ import annotations

import io
import json
import tarfile
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

from github_ecosystem_miner.config import CollectionConfig
from github_ecosystem_miner.errors import CollectionError
from github_ecosystem_miner.github_client import GitHubClient
from github_ecosystem_miner.storage import connect

CONFIG_MARKERS = {
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    ".dockerignore",
    ".pre-commit-config.yaml",
    "tox.ini",
    "noxfile.py",
    "poetry.lock",
    "pipfile",
    "uv.lock",
    "pdm.lock",
    "mkdocs.yml",
    ".readthedocs.yml",
    ".gitlab-ci.yml",
    "jenkinsfile",
    ".travis.yml",
    "azure-pipelines.yml",
    "dvc.yaml",
    "vercel.json",
    "cmakelists.txt",
    "snakefile",
    "package.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
}


@dataclass(frozen=True)
class TreeEntry:
    path: str
    type: str
    sha: str
    size: int | None = None
    mode: str | None = None


@dataclass(frozen=True)
class TreeResult:
    entries: tuple[TreeEntry, ...]
    truncated: bool
    api_requests: int
    bytes_transferred: int
    cache_hit: bool


@dataclass(frozen=True)
class FileResult:
    path: str
    blob_sha: str
    content: bytes
    source: str
    cache_hit: bool
    bytes_transferred: int


@dataclass(frozen=True)
class RetrievalResult:
    files: tuple[FileResult, ...]
    strategy: str
    archive_used: bool
    file_requests: int
    cache_hits: int
    bytes_transferred: int
    duration_ms: float


def is_relevant_path(path: str, dependency_files: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    lower = normalized.lower()
    name = PurePosixPath(lower).name
    dependency_names = {PurePosixPath(item.lower()).name for item in dependency_files}
    if name in dependency_names or name in CONFIG_MARKERS:
        return True
    if lower.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
        return True
    if name.endswith(".tf"):
        return True
    if any(part in {"k8s", "kubernetes", "helm"} for part in PurePosixPath(lower).parts):
        return name.endswith((".yml", ".yaml", ".json")) or name == "chart.yaml"
    return False


def relevant_entries(
    entries: tuple[TreeEntry, ...], dependency_files: tuple[str, ...]
) -> tuple[TreeEntry, ...]:
    return tuple(
        entry
        for entry in entries
        if entry.type == "blob" and is_relevant_path(entry.path, dependency_files)
    )


def discover_tree(
    database_path,
    client: GitHubClient,
    *,
    repository_id: int,
    full_name: str,
    commit_sha: str,
    tree_sha: str,
    config: CollectionConfig,
) -> TreeResult:
    with connect(database_path, read_only=True) as connection:
        cached = connection.execute(
            """
            SELECT entries_json, truncated, api_requests, bytes_transferred
            FROM repository_tree_cache
            WHERE repository_id = ? AND commit_sha = ? AND tree_sha = ?
            """,
            [repository_id, commit_sha, tree_sha],
        ).fetchone()
    if cached:
        return TreeResult(
            entries=tuple(TreeEntry(**entry) for entry in json.loads(cached[0])),
            truncated=bool(cached[1]),
            api_requests=0,
            bytes_transferred=0,
            cache_hit=True,
        )

    if not config.tree_recursive:
        entries, request_count, byte_count, truncated = _walk_tree(
            client,
            full_name,
            tree_sha,
            max_calls=config.tree_fallback_max_calls,
            max_depth=config.tree_fallback_max_depth,
        )
        recursive = None
    else:
        recursive = client.rest_get(
            f"/repos/{full_name}/git/trees/{tree_sha}",
            params={"recursive": 1},
            resource="core",
        )
        request_count = 1
        byte_count = _json_size(recursive) if recursive else 0
        entries = _tree_entries((recursive or {}).get("tree") or [])
        truncated = bool((recursive or {}).get("truncated"))
    if recursive is None:
        if config.tree_recursive:
            raise CollectionError(f"Tree {tree_sha} was not found")

    if truncated:
        entries, fallback_requests, fallback_bytes, fallback_truncated = _walk_tree(
            client,
            full_name,
            tree_sha,
            max_calls=config.tree_fallback_max_calls,
            max_depth=config.tree_fallback_max_depth,
        )
        request_count += fallback_requests
        byte_count += fallback_bytes
        truncated = fallback_truncated

    encoded = json.dumps([entry.__dict__ for entry in entries], sort_keys=True)
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO repository_tree_cache
                (repository_id, commit_sha, tree_sha, entries_json, truncated,
                 api_requests, bytes_transferred)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [repository_id, commit_sha, tree_sha, encoded, truncated, request_count, byte_count],
        )
    return TreeResult(tuple(entries), truncated, request_count, byte_count, False)


def _walk_tree(
    client: GitHubClient,
    full_name: str,
    tree_sha: str,
    *,
    max_calls: int,
    max_depth: int,
) -> tuple[list[TreeEntry], int, int, bool]:
    queue: list[tuple[str, str, int]] = [(tree_sha, "", 0)]
    result: list[TreeEntry] = []
    requests = 0
    bytes_transferred = 0
    incomplete = False
    while queue:
        sha, prefix, depth = queue.pop(0)
        if requests >= max_calls:
            incomplete = True
            break
        body = client.rest_get(f"/repos/{full_name}/git/trees/{sha}", resource="core")
        requests += 1
        if body is None:
            incomplete = True
            continue
        bytes_transferred += _json_size(body)
        for raw in body.get("tree") or []:
            path = f"{prefix}/{raw['path']}".strip("/")
            entry = TreeEntry(
                path=path,
                type=raw.get("type", ""),
                sha=raw.get("sha", ""),
                size=raw.get("size"),
                mode=raw.get("mode"),
            )
            result.append(entry)
            if entry.type == "tree":
                if depth < max_depth:
                    queue.append((entry.sha, path, depth + 1))
                else:
                    incomplete = True
    return result, requests, bytes_transferred, incomplete


def retrieve_relevant_files(
    database_path,
    client: GitHubClient,
    *,
    repository_id: int,
    full_name: str,
    commit_sha: str,
    repository_size_kb: int,
    entries: tuple[TreeEntry, ...],
    dependency_files: tuple[str, ...],
    config: CollectionConfig,
) -> RetrievalResult:
    started = time.perf_counter()
    selected = relevant_entries(entries, dependency_files)
    cached_files: list[FileResult] = []
    missing: list[TreeEntry] = []
    for entry in selected:
        cached = _load_file_cache(database_path, repository_id, commit_sha, entry)
        if cached:
            cached_files.append(cached)
        else:
            missing.append(entry)

    use_archive = bool(missing) and _should_use_archive(
        len(selected), repository_size_kb, config
    )
    fetched: list[FileResult] = []
    file_requests = 0
    transferred = 0
    strategy = "cache"
    if use_archive:
        try:
            archive_files, archive_bytes = _retrieve_archive(
                client,
                full_name=full_name,
                commit_sha=commit_sha,
                selected=missing,
                config=config,
            )
            file_requests = 1
            transferred += archive_bytes
            strategy = "archive"
            fetched.extend(archive_files)
            fetched_paths = {item.path for item in archive_files}
            missing = [item for item in missing if item.path not in fetched_paths]
        except CollectionError:
            use_archive = False

    if missing:
        strategy = "archive+selective_raw" if use_archive else "selective_raw"
        for entry in missing:
            result = _retrieve_file(
                client,
                full_name=full_name,
                commit_sha=commit_sha,
                entry=entry,
                max_bytes=config.raw_file_max_bytes,
            )
            file_requests += 1 if result else 0
            if result:
                transferred += result.bytes_transferred
                fetched.append(result)

    for result in fetched:
        _save_file_cache(database_path, repository_id, commit_sha, result)
    duration_ms = (time.perf_counter() - started) * 1000
    return RetrievalResult(
        files=tuple(cached_files + fetched),
        strategy=strategy,
        archive_used=use_archive,
        file_requests=file_requests,
        cache_hits=len(cached_files),
        bytes_transferred=transferred,
        duration_ms=duration_ms,
    )


def _retrieve_file(
    client: GitHubClient,
    *,
    full_name: str,
    commit_sha: str,
    entry: TreeEntry,
    max_bytes: int,
) -> FileResult | None:
    if entry.size is not None and entry.size > max_bytes:
        return None
    owner, name = full_name.split("/", 1)
    raw_url = (
        f"https://raw.githubusercontent.com/{quote(owner)}/{quote(name)}/"
        f"{quote(commit_sha)}/{quote(entry.path, safe='/')}"
    )
    content = client.get_bytes(raw_url, allow_not_found=True)
    source = "raw_commit_url"
    if content is None:
        content = client.get_bytes(
            f"https://api.github.com/repos/{owner}/{name}/git/blobs/{entry.sha}",
            api="rest",
            resource="core",
            endpoint=f"/repos/{owner}/{name}/git/blobs/{entry.sha}",
            allow_not_found=True,
            accept="application/vnd.github.raw+json",
        )
        source = "git_blob_api"
    if content is None or len(content) > max_bytes:
        return None
    return FileResult(entry.path, entry.sha, content, source, False, len(content))


def _should_use_archive(file_count: int, repository_size_kb: int, config: CollectionConfig) -> bool:
    if config.archive_mode == "never":
        return False
    if repository_size_kb > config.archive_max_repository_kb:
        return False
    return config.archive_mode == "always" or file_count >= config.archive_file_threshold


def _retrieve_archive(
    client: GitHubClient,
    *,
    full_name: str,
    commit_sha: str,
    selected: list[TreeEntry],
    config: CollectionConfig,
) -> tuple[list[FileResult], int]:
    owner, name = full_name.split("/", 1)
    archive = client.get_bytes(
        f"https://api.github.com/repos/{owner}/{name}/tarball/{commit_sha}",
        api="rest",
        resource="core",
        endpoint=f"/repos/{owner}/{name}/tarball/{commit_sha}",
        accept="application/vnd.github+json",
    )
    if archive is None:
        raise CollectionError(f"Archive was unavailable for {full_name}@{commit_sha}")
    if len(archive) > config.archive_max_download_bytes:
        raise CollectionError("Archive exceeds configured download limit")
    wanted = {entry.path: entry for entry in selected}
    extracted: list[FileResult] = []
    total_size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) > config.archive_max_files:
                raise CollectionError("Archive contains too many entries")
            for member in members:
                if member.issym() or member.islnk() or member.isdev():
                    continue
                parts = PurePosixPath(member.name).parts
                if len(parts) < 2 or any(part == ".." for part in parts):
                    continue
                relative = PurePosixPath(*parts[1:]).as_posix()
                entry = wanted.get(relative)
                if entry is None or not member.isfile():
                    continue
                total_size += member.size
                if total_size > config.archive_max_extracted_bytes:
                    raise CollectionError("Archive exceeds configured extraction limit")
                if member.size > config.raw_file_max_bytes:
                    continue
                handle = bundle.extractfile(member)
                content = handle.read(config.raw_file_max_bytes + 1) if handle else b""
                if len(content) > config.raw_file_max_bytes:
                    continue
                extracted.append(
                    FileResult(relative, entry.sha, content, "archive", False, len(content))
                )
    except (tarfile.TarError, OSError) as error:
        raise CollectionError(f"Unsafe or invalid repository archive: {error}") from error
    if not extracted:
        raise CollectionError("Archive contained none of the selected files")
    return extracted, len(archive)


def _load_file_cache(
    database_path, repository_id: int, commit_sha: str, entry: TreeEntry
) -> FileResult | None:
    with connect(database_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT content, retrieval_source
            FROM repository_file_cache
            WHERE repository_id = ? AND commit_sha = ? AND path = ? AND blob_sha = ?
            """,
            [repository_id, commit_sha, entry.path, entry.sha],
        ).fetchone()
    if not row:
        return None
    content = bytes(row[0])
    return FileResult(entry.path, entry.sha, content, str(row[1]), True, 0)


def _save_file_cache(
    database_path, repository_id: int, commit_sha: str, result: FileResult
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO repository_file_cache
                (repository_id, commit_sha, path, blob_sha, content,
                 byte_size, retrieval_source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                repository_id,
                commit_sha,
                result.path,
                result.blob_sha,
                result.content,
                len(result.content),
                result.source,
            ],
        )


def _tree_entries(raw_entries: list[dict]) -> list[TreeEntry]:
    return [
        TreeEntry(
            path=str(item.get("path", "")),
            type=str(item.get("type", "")),
            sha=str(item.get("sha", "")),
            size=item.get("size"),
            mode=item.get("mode"),
        )
        for item in raw_entries
    ]


def _json_size(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
