import unittest

from github_ecosystem_miner.errors import CollectionError
from github_ecosystem_miner.rate_limits import RateLimitManager


class TestRateLimitManager(unittest.TestCase):
    def test_tracks_core_search_and_graphql_independently(self):
        manager = RateLimitManager(
            1,
            reserves={"core": 5, "search": 1, "graphql": 10},
            wait_enabled=False,
            clock=lambda: 100.0,
        )
        manager.observe_headers(
            0,
            "search",
            {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "200"},
        )
        manager.observe_headers(
            0,
            "core",
            {"X-RateLimit-Remaining": "500", "X-RateLimit-Reset": "200"},
        )
        self.assertTrue(manager.token_available(0, "core"))
        self.assertFalse(manager.token_available(0, "search"))
        with self.assertRaises(CollectionError):
            manager.before_request(0, "search")

    def test_retry_after_creates_shared_secondary_cooldown(self):
        sleeps = []
        now = [100.0]
        manager = RateLimitManager(
            2,
            sleeper=lambda delay: (sleeps.append(delay), now.__setitem__(0, now[0] + delay)),
            clock=lambda: now[0],
        )
        manager.mark_secondary_limit(12)
        manager.before_request(1, "graphql")
        self.assertEqual(sleeps, [12])


if __name__ == "__main__":
    unittest.main()
