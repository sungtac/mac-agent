import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from edge_agent_capability_registry import discover_skill_ids, resolve, select_skill_ids


class CapabilityRegistryTests(unittest.TestCase):
    def test_discovers_all_skill_manifests(self):
        skills = discover_skill_ids()
        self.assertIn("edge-agent-behavior", skills)
        self.assertIn("calendar", skills)
        self.assertIn("code-review", skills)

    def test_same_prompt_resolves_same_context(self):
        first = resolve("최저가 제품 추천과 출처를 찾아줘")
        second = resolve("최저가 제품 추천과 출처를 찾아줘")
        self.assertEqual(first.capability_ids, second.capability_ids)
        self.assertEqual(first.context, second.context)
        self.assertIn("product_research", first.capability_ids)

    def test_context_is_bounded(self):
        result = resolve("코드 리뷰와 검증을 해줘", max_chars=1200)
        self.assertLessEqual(len(result.context), 1200)
        self.assertIn("edge-agent-behavior", result.capability_ids)


if __name__ == "__main__":
    unittest.main()
