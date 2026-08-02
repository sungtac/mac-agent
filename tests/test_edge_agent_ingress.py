import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_ingress import classify, is_deliberation_request  # noqa: E402


class IngressRoutingTests(unittest.TestCase):
    def test_only_named_agent_receives_direct_address(self):
        decision = classify("클로드야 오늘 무안군 날씨 알려줘")
        self.assertEqual(decision.route, "targeted")
        self.assertTrue(decision.accepts("claude"))
        self.assertFalse(decision.accepts("codex"))
        self.assertFalse(decision.accepts("antigravity"))
        self.assertFalse(decision.accepts("roda"))

    def test_roda_address_is_exclusive(self):
        decision = classify("로다야 오늘 무안군 날씨 알려줘")
        self.assertEqual(decision.targets, frozenset({"roda"}))
        self.assertTrue(decision.accepts("roda"))
        self.assertFalse(decision.accepts("claude"))

    def test_plain_group_message_reaches_all_agents(self):
        decision = classify("안녕")
        self.assertEqual(decision.route, "default")
        self.assertTrue(all(decision.accepts(role) for role in ("claude", "codex", "antigravity", "roda")))

    def test_plain_greeting_reaches_all_agents(self):
        decision = classify("안녕하세요")
        self.assertTrue(all(decision.accepts(role) for role in ("claude", "codex", "antigravity", "roda")))

    def test_group_address_is_explicit_fanout(self):
        decision = classify("안녕 얘들아")
        self.assertEqual(decision.route, "broadcast")
        self.assertTrue(all(decision.accepts(role) for role in ("claude", "codex", "antigravity", "roda")))

    def test_unrelated_bot_command_is_blocked(self):
        decision = classify("/start@some_other_bot 안녕")
        self.assertEqual(decision.route, "blocked")
        self.assertFalse(decision.accepts("claude"))

    def test_mid_sentence_bare_name_is_not_address(self):
        decision = classify("어제 클로드가 이상했어")
        self.assertEqual(decision.route, "default")
        self.assertTrue(decision.accepts("claude"))
        self.assertTrue(decision.accepts("codex"))

    def test_coordinated_role_addresses_include_the_second_role(self):
        decision = classify("안티랑 로다는 왜 안해?")
        self.assertEqual(decision.route, "targeted")
        self.assertEqual(decision.targets, frozenset({"antigravity", "roda"}))
        self.assertTrue(decision.accepts("antigravity"))
        self.assertTrue(decision.accepts("roda"))

    def test_deliberation_marker_is_detected_without_changing_routing(self):
        self.assertTrue(is_deliberation_request("너희들이 30일 안에 수익을 만드는 방법들을 논의해"))
        self.assertFalse(is_deliberation_request("안녕하세요"))


if __name__ == "__main__":
    unittest.main()
