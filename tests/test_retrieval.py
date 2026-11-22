import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from github_ecosystem_miner.config import CollectionConfig
from github_ecosystem_miner.retrieval import (
    TreeEntry,
    _retrieve_archive,
    discover_tree,
    retrieve_relevant_files,
)
from github_ecosystem_miner.storage import initialize_database


class TreeClient:
    def __init__(self):
        self.calls = []

    def rest_get(self, endpoint, *, params=None, resource="core", allow_not_found=False):
        self.calls.append((endpoint, params))
        if params == {"recursive": 1}:
            return {"truncated": True, "tree": []}
        if endpoint.endswith("/root"):
            return {
                "tree": [
                    {"path": "requirements.txt", "type": "blob", "sha": "req", "size": 8},
                    {"path": "services", "type": "tree", "sha": "child"},
                ]
            }
        return {
            "tree": [
                {"path": "pyproject.toml", "type": "blob", "sha": "project", "size": 12}
            ]
        }


class FileClient:
    def __init__(self):
        self.calls = 0

    def get_bytes(self, url, **kwargs):
        self.calls += 1
        return b"fastapi==1"


class ArchiveClient:
    def __init__(self, content):
        self.content = content

    def get_bytes(self, url, **kwargs):
        return self.content


class TestRetrieval(unittest.TestCase):
    def test_truncated_recursive_tree_uses_bounded_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.duckdb"
            initialize_database(database)
            client = TreeClient()
            result = discover_tree(
                database,
                client,
                repository_id=1,
                full_name="owner/repo",
                commit_sha="commit",
                tree_sha="root",
                config=CollectionConfig(),
            )
            self.assertFalse(result.truncated)
            self.assertEqual(
                {entry.path for entry in result.entries},
                {"requirements.txt", "services", "services/pyproject.toml"},
            )
            self.assertEqual(result.api_requests, 3)

    def test_file_cache_avoids_second_download(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.duckdb"
            initialize_database(database)
            client = FileClient()
            entries = (TreeEntry("requirements.txt", "blob", "blob", 10),)
            kwargs = {
                "repository_id": 1,
                "full_name": "owner/repo",
                "commit_sha": "commit",
                "repository_size_kb": 10,
                "entries": entries,
                "dependency_files": ("requirements.txt",),
                "config": CollectionConfig(archive_mode="never"),
            }
            first = retrieve_relevant_files(database, client, **kwargs)
            second = retrieve_relevant_files(database, client, **kwargs)
            self.assertEqual(client.calls, 1)
            self.assertEqual(first.cache_hits, 0)
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(second.file_requests, 0)

    def test_archive_ignores_traversal_and_links(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            valid = b"fastapi"
            info = tarfile.TarInfo("repo-root/requirements.txt")
            info.size = len(valid)
            archive.addfile(info, io.BytesIO(valid))
            traversal = tarfile.TarInfo("repo-root/../outside.txt")
            traversal.size = 3
            archive.addfile(traversal, io.BytesIO(b"bad"))
            link = tarfile.TarInfo("repo-root/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside"
            archive.addfile(link)
        files, _ = _retrieve_archive(
            ArchiveClient(stream.getvalue()),
            full_name="owner/repo",
            commit_sha="commit",
            selected=[TreeEntry("requirements.txt", "blob", "blob", 7)],
            config=CollectionConfig(),
        )
        self.assertEqual([item.path for item in files], ["requirements.txt"])


if __name__ == "__main__":
    unittest.main()
