import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_context_budget import bound_items, bound_text, budget_for, compress_sections, estimate_input_tokens  # noqa: E402


class ContextBudgetTests(unittest.TestCase):
    def test_profiles_have_increasing_room(self):
        self.assertLess(budget_for("chat").max_context_chars, budget_for("full_review").max_context_chars)

    def test_bound_text_preserves_tail_for_logs(self):
        value = bound_text("start-" + ("x" * 100) + "-error-tail", 30, tail=True)
        self.assertLessEqual(len(value), 30)
        self.assertIn("error-tail", value)

    def test_context_rejects_sensitive_markers_and_estimates_only(self):
        with self.assertRaises(ValueError):
            bound_text("authorization: bearer hidden", 100)
        self.assertEqual(estimate_input_tokens("1234"), 1)
        self.assertEqual(bound_items(["a", "b", "c"], 10, 2), ["b", "c"])

    def test_compression_preserves_high_priority_request_and_budget(self):
        rendered = compress_sections(
            (("evidence", "e" * 500, 1), ("user_request", "요청 핵심", 5)),
            80,
        )
        self.assertLessEqual(len(rendered), 80)
        self.assertIn("요청 핵심", rendered)
        self.assertIn("[user_request]", rendered)

    def test_compression_rejects_secret_before_truncation(self):
        with self.assertRaises(ValueError):
            compress_sections((("evidence", "prefix " + "secret=hidden", 1),), 20)


if __name__ == "__main__":
    unittest.main()
