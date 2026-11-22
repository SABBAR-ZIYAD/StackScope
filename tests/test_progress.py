import unittest

from github_ecosystem_miner.progress import progress_message


class TestProgressMessages(unittest.TestCase):
    def test_progress_includes_stage_count_percentage_and_detail(self):
        message = progress_message("enrich", 25, 100, "owner/repository")
        self.assertEqual(message, "[enrich] 25/100 ( 25.0%) | owner/repository")

    def test_empty_stage_is_complete(self):
        self.assertEqual(progress_message("clean", 0, 0), "[clean] 0/0 (100.0%)")


if __name__ == "__main__":
    unittest.main()
