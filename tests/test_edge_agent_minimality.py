import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_minimality import review_for  # noqa: E402


class MinimalityTests(unittest.TestCase):
    def test_full_mode_is_review_only_and_preserves_guards(self):
        review = review_for(mode="full")
        self.assertTrue(review.applicable)
        self.assertIn("입력 검증", review.protected_guards)
        self.assertIn("기존 코드베이스", review.questions[1])

    def test_sensitive_and_operational_tasks_are_excluded(self):
        self.assertFalse(review_for(mode="ultra", sensitive_path=True).applicable)
        self.assertFalse(review_for(mode="full", task_kind="ops").applicable)

    def test_off_mode_has_no_questions(self):
        review = review_for(mode="off")
        self.assertFalse(review.applicable)
        self.assertEqual(review.questions, ())


if __name__ == "__main__":
    unittest.main()
