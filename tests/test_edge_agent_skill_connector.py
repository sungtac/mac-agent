import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from edge_agent_skill_connector import build_skill_context, select_skill_ids


class SkillConnectorTests(unittest.TestCase):
    def test_always_includes_provider_neutral_behavior_contract(self):
        context = build_skill_context("간단한 상태 확인", max_chars=6000)
        self.assertIn("edge-agent-behavior", context)
        self.assertIn("Success criteria", context)

    def test_selects_domain_skill_without_external_access(self):
        self.assertIn("product_research", select_skill_ids("최저가 제품 추천해줘"))
        self.assertIn("calendar", select_skill_ids("내일 일정 추가"))

    def test_context_is_bounded_and_contract_only(self):
        context = build_skill_context("토큰 한도 때문에 작업 재개", max_chars=1200)
        self.assertLessEqual(len(context), 1200)
        self.assertIn("quota_resume", context)
        self.assertNotIn(".openclaw/workspace", context)


if __name__ == "__main__":
    unittest.main()
