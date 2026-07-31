import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_skill_policy import build_skill_context, select_skill_ids  # noqa: E402


class SkillPolicyTests(unittest.TestCase):
    def test_selects_only_relevant_skills(self):
        self.assertEqual(select_skill_ids("컨텍스트를 compact하고 검증해줘"), ("context_budget", "verification"))

    def test_bounds_injected_skill_context(self):
        result = build_skill_context(
            "ponytail review",
            {"minimality_review": "rule\n" * 20},
            max_chars=120,
        )
        self.assertLessEqual(len(result.context), 120)
        self.assertIn("minimality_review", result.skill_ids)

    def test_missing_documents_are_reported(self):
        result = build_skill_context("quota and verification", {"verification": "verify"})
        self.assertIn("quota_routing", result.omitted)


if __name__ == "__main__":
    unittest.main()
