import unittest
from unittest.mock import patch

import httpx

from github_ecosystem_miner.collection import (
    build_enrichment_query,
    build_search_query,
    split_star_range,
)
from github_ecosystem_miner.config import CollectionConfig
from github_ecosystem_miner.github_client import GitHubClient, retry_decision


class TestSearchSlicing(unittest.TestCase):
    def test_query_owns_star_range(self):
        query = build_search_query("topic:webapp fork:false", 100, 250)
        self.assertEqual(query, "topic:webapp fork:false stars:100..250")

    def test_split_is_complete_and_non_overlapping(self):
        lower, upper = split_star_range(100, 400)
        self.assertEqual(lower, (100, 250))
        self.assertEqual(upper, (251, 400))

    def test_graphql_batches_only_snapshot_metadata(self):
        query = build_enrichment_query(
            ["owner/one", "owner/two"], ["requirements.txt", "pyproject.toml"]
        )
        self.assertIn("r0: repository", query)
        self.assertIn("r1: repository", query)
        self.assertIn("defaultBranchRef", query)
        self.assertIn("languages(first: 100", query)
        self.assertNotIn("HEAD:requirements.txt", query)
        self.assertIn("rateLimit", query)


class TestRetryDecision(unittest.TestCase):
    def test_permission_error_is_not_retried(self):
        result = retry_decision(403, {"X-RateLimit-Remaining": "100"}, "forbidden", 0)
        self.assertEqual(result.action, "fail")
        self.assertEqual(result.reason, "permission_denied")

    def test_primary_limit_waits_for_reset(self):
        result = retry_decision(
            403,
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"},
            "rate limit",
            0,
            now=1000,
        )
        self.assertEqual(result.action, "wait_reset")
        self.assertEqual(result.reset_epoch, 1100)

    def test_server_errors_back_off(self):
        first = retry_decision(502, {}, "bad gateway", 0)
        second = retry_decision(502, {}, "bad gateway", 1)
        self.assertEqual(first.action, "wait")
        self.assertGreater(second.delay_seconds, first.delay_seconds)

    def test_client_retries_transient_server_error(self):
        attempts = []

        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json={"ok": True})

        client = GitHubClient(
            CollectionConfig(max_retries=2), transport=httpx.MockTransport(handler)
        )
        try:
            with patch("github_ecosystem_miner.github_client.time.sleep"):
                self.assertEqual(client.rest_get("/test"), {"ok": True})
        finally:
            client.close()
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
