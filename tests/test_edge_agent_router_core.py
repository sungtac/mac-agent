import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_router_core import route  # noqa: E402


class RouterCoreTests(unittest.TestCase):
    def test_simple_explanation_is_single_provider(self):
        decision = route("화자 분리가 무슨 뜻이야?")
        self.assertEqual(decision.task_type, "explanation")
        self.assertEqual(decision.execution_mode, "single")
        self.assertEqual(len(decision.roles), 1)


    def test_explicit_provider_and_role_order_are_preserved(self):
        decision = route("코덱스가 작성하고 안티그래비티가 검토해줘.")
        self.assertEqual([(item.provider.value, item.role.value) for item in decision.roles], [
            ("codex", "writer"),
            ("antigravity", "reviewer"),
        ])
        self.assertEqual(decision.execution_mode, "small_team")


    def test_complex_document_request_becomes_team(self):
        decision = route("공고문, 사업계획서, 견적서를 분석해서 제출용 제안서를 작성하고 검토해줘.", attachment_count=3)
        self.assertEqual(decision.execution_mode, "team")
        self.assertEqual({item.provider.value for item in decision.roles}, {"gemma", "antigravity", "codex", "claude"})
        self.assertEqual([item.role.value for item in decision.roles], [
            "source_extractor", "researcher", "implementer", "integrator"
        ])

    def test_explicit_deliberation_assigns_all_four_providers(self):
        decision = route("지금부터 30일 안에 100만원 수익을 만들 방법들을 너희들이 논의해")
        self.assertEqual(decision.execution_mode, "team")
        self.assertEqual(
            {item.provider.value for item in decision.roles},
            {"claude", "codex", "antigravity", "gemma"},
        )
        self.assertEqual(decision.roles[-1].provider.value, "claude")
        self.assertEqual(decision.roles[-1].role.value, "integrator")


    def test_claude_forbidden_is_honored(self):
        decision = route("Claude는 사용하지 말고 이 기술안을 분석하고 검토해줘.")
        self.assertTrue(decision.claude_forbidden)
        self.assertTrue(all(item.provider.value != "claude" for item in decision.roles))
        self.assertEqual(decision.roles[0].role.value, "reviewer")
        self.assertEqual(decision.roles[0].provider.value, "antigravity")

    def test_document_improvement_requires_small_team(self):
        decision = route("이 제안서 내용을 전문적으로 개선해줘.")
        self.assertEqual(decision.task_type, "document")
        self.assertEqual(decision.execution_mode, "small_team")
        self.assertEqual([item.role.value for item in decision.roles], ["writer", "reviewer"])


    def test_high_risk_plan_requires_approval_but_does_not_execute(self):
        decision = route("운영 서비스를 재시작하고 배포해줘.")
        self.assertEqual(decision.risk_level, "high")
        self.assertTrue(decision.requires_approval)
        self.assertFalse(decision.requires_worktree)


    def test_same_input_is_deterministic(self):
        text = "첨부 자료를 비교하고 누락된 내용을 검토해줘."
        self.assertEqual(route(text).to_dict(), route(text).to_dict())


if __name__ == "__main__":
    unittest.main()
