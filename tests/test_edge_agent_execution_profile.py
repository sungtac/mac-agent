import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_execution_profile import choose_profile, provider_args  # noqa: E402


class ExecutionProfileTests(unittest.TestCase):
    def test_chat_is_cheaper_than_full_review(self):
        chat = choose_profile("chat", provider="codex")
        full = choose_profile("full_review", provider="claude")
        self.assertLess(chat.max_turns, full.max_turns)
        self.assertEqual(chat.reasoning, "low")
        self.assertEqual(full.reasoning, "high")

    def test_options_are_provider_specific(self):
        self.assertIn("model", provider_args(choose_profile("research", provider="claude")))
        self.assertIn("reasoning_effort", provider_args(choose_profile("nano_mid", provider="codex")))

    def test_profiles_do_not_enable_automatic_merge(self):
        self.assertFalse(choose_profile("nano_light", provider="codex").automatic_merge)


if __name__ == "__main__":
    unittest.main()
