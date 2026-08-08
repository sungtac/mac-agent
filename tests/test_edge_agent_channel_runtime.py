import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import edge_agent_channel_runtime as runtime  # noqa: E402


class ChannelRuntimeTests(unittest.TestCase):
    def setUp(self):
        with runtime._PREFLIGHT_CACHE_LOCK:
            runtime._PREFLIGHT_CACHE.clear()

    def test_terminal_and_telegram_use_identical_shared_context(self):
        common = {
            "request": "온라인 자료를 조사해줘",
            "provider": "codex",
            "workspace": "/tmp/worktree",
            "session_context": "[세션 맥락]\n같은 작업",
        }
        with patch.object(runtime, "render_team_contract", return_value="TEAM"), \
                patch.object(runtime, "render_capability_preflight", return_value="CAPABILITY"), \
                patch.object(runtime, "build_skill_context", return_value="SKILL"), \
                patch.object(runtime, "render_agent_profile", return_value="IDENTITY"), \
                patch.object(runtime, "render_routing_context", return_value="ROUTING"):
            terminal = runtime.build_prompt(**common)
            telegram = runtime.build_prompt(**common)
        self.assertEqual(terminal, telegram)
        self.assertIn("TEAM", terminal)
        self.assertIn("IDENTITY", terminal)
        self.assertIn("ROUTING", terminal)
        self.assertIn("[사용자 요청]", terminal)

    def test_antigravity_headless_policy_is_common_and_bounded(self):
        with patch.object(runtime, "render_team_contract", return_value="TEAM"), \
                patch.object(runtime, "render_capability_preflight", return_value="CAPABILITY"), \
                patch.object(runtime, "build_skill_context", return_value="SKILL"), \
                patch.object(runtime, "render_agent_profile", return_value="IDENTITY"), \
                patch.object(runtime, "render_routing_context", return_value="ROUTING"):
            prompt = runtime.build_prompt(
                "출처를 확인해줘",
                provider="agy",
                workspace="/tmp/worktree",
                headless=True,
            )
        self.assertIn("헤드리스 안전 실행 규칙", prompt)
        self.assertIn("unsandboxed", prompt)

    def test_explicit_provider_selection_is_reflected_in_shared_routing(self):
        codex = runtime.render_routing_context("돌핀 태풍의 진행 상황 알려줘", provider="codex")
        antigravity = runtime.render_routing_context("돌핀 태풍의 진행 상황 알려줘", provider="antigravity")
        self.assertIn("researcher=codex", codex)
        self.assertIn("researcher=antigravity", antigravity)

    def test_long_request_is_bounded_only_for_routing(self):
        request = "돌핀 태풍의 진행 상황 알려줘 " + ("x" * 5000)
        with patch.object(runtime, "route_request") as route_request:
            decision = type(
                "Decision",
                (),
                {"to_dict": lambda self: {
                    "task_type": "research", "risk_level": "low",
                    "execution_mode": "read_only", "roles": [],
                    "requires_worktree": False,
                }},
            )()
            route_request.return_value = decision
            rendered = runtime.render_routing_context(request, provider="codex")
        routed_request = route_request.call_args.args[0].text
        self.assertEqual(len(routed_request), 4000)
        self.assertIn("작업 유형: research", rendered)

    def test_roda_does_not_run_external_capability_probes(self):
        with patch.object(runtime, "render_capability_preflight") as preflight:
            prompt = runtime.build_prompt("간단히 답해줘", provider="roda", channel="telegram")
        preflight.assert_not_called()
        self.assertIn("Roda capability boundary", prompt)

    def test_capability_preflight_is_bounded_by_short_ttl_cache(self):
        with patch.object(runtime, "render_capability_preflight", return_value="CAPABILITY") as preflight, \
                patch.object(runtime, "build_skill_context", return_value=""), \
                patch.object(runtime, "render_team_contract", return_value="TEAM"), \
                patch.object(runtime, "render_agent_profile", return_value="IDENTITY"), \
                patch.object(runtime, "render_routing_context", return_value="ROUTING"):
            runtime.build_shared_context("상태 확인", provider="codex", workspace="/tmp/worktree")
            runtime.build_shared_context("상태 확인", provider="codex", workspace="/tmp/worktree")
        self.assertEqual(preflight.call_count, 1)

    def test_unknown_channel_is_rejected(self):
        with self.assertRaises(ValueError):
            runtime.build_prompt("테스트", provider="codex", channel="unknown")


if __name__ == "__main__":
    unittest.main()
