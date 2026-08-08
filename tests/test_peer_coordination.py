from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))


def load(name: str, filename: str, **env):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    with patch.dict(os.environ, env):
        spec.loader.exec_module(module)
    return module


class PeerCoordinationTests(unittest.TestCase):
    def test_roda_hardens_launchd_logs_before_token_checks(self):
        source = (BIN / "roda-gemma-bot.py").read_text(encoding="utf-8")
        main_offset = source.index("def main()")
        self.assertLess(source.index("_harden_log_permissions()", main_offset), source.index("TOKEN_FILE.is_file", main_offset))

    def test_roda_can_render_unresolved_health_incidents(self):
        token_root = tempfile.TemporaryDirectory()
        token = Path(token_root.name) / "telegram.token"
        token.write_text("123456:test-token", encoding="utf-8")
        token.chmod(0o600)
        try:
            roda = load(
                "incident_query_roda",
                "roda-gemma-bot.py",
                RODA_GEMMA_TOKEN_FILE=str(token),
            )
            with tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "health.json"
                state.write_text(
                    '{"incidents":{"inc-1":{"incident_id":"inc-1","role":"codex","code":"no_response","status":"open","task_id":"task-1","last_seen_at":2},"inc-2":{"incident_id":"inc-2","role":"claude","code":"execution_error","status":"resolved","last_seen_at":1}}}',
                    encoding="utf-8",
                )
                rendered = roda._render_unresolved_incidents(state)
            self.assertTrue(roda._is_unresolved_incident_query("지금까지 해결되지 않은 모든 오류를 알려줘"))
            self.assertIn("미해결 장애는 1건", rendered)
            self.assertIn("task=task-1", rendered)
            self.assertNotIn("execution_error", rendered)
        finally:
            token_root.cleanup()

    def test_current_status_query_does_not_follow_expired_topic_anchor(self):
        from edge_agent_context_envelope import ContextEnvelopeStore

        with tempfile.TemporaryDirectory() as directory:
            store = ContextEnvelopeStore(directory)
            store.prepare(
                channel="telegram", provider="claude", chat_id="-1", message_id="1",
                reply_to_message_id=None, text="영상 https://example.com/topic",
            )
            preparation = store.prepare(
                channel="telegram", provider="claude", chat_id="-1", message_id="2",
                reply_to_message_id=None, text="안티랑 로다는 현재 상태를 각각 확인하고 합쳐서 알려줘",
            )
        self.assertFalse(preparation.guard_required)

    def test_peer_snapshot_is_bounded_and_secret_free(self):
        from edge_agent_peer_context import snapshot

        with patch("edge_agent_peer_context._launchctl_state", return_value="running"), patch(
            "edge_agent_peer_context.latest_task_state",
            return_value={
                "status": "completed",
                "updated_at": "2026-08-02T00:00:00+00:00",
                "request_tail": "상태 확인",
                "response_tail": "정상",
                "token": "123456:must-not-appear",
            },
        ):
            rendered = snapshot(max_chars=1000)

        self.assertLessEqual(len(rendered), 1000)
        self.assertIn("claude", rendered)
        self.assertIn("codex", rendered)
        self.assertIn("antigravity", rendered)
        self.assertIn("roda", rendered)
        self.assertNotIn("must-not-appear", rendered)

    def test_failed_last_task_is_not_current_service_failure(self):
        from edge_agent_peer_context import snapshot

        with patch("edge_agent_peer_context._launchctl_state", return_value="running"), patch(
            "edge_agent_peer_context.latest_task_state",
            return_value={"status": "failed", "updated_epoch": 0, "updated_at": "old"},
        ):
            rendered = snapshot(max_chars=1000)
        self.assertIn("service=running", rendered)
        self.assertIn("activity=idle", rendered)
        self.assertIn("last_task=failed", rendered)

    def test_explicit_conjunction_routes_to_coordinator_and_roda(self):
        token_root = tempfile.TemporaryDirectory()
        token = Path(token_root.name) / "telegram.token"
        token.write_text("123456:test-token", encoding="utf-8")
        token.chmod(0o600)
        try:
            bot = load(
                "peer_coordination_telegram",
                "telegram-agent-bot.py",
                TELEGRAM_AGENT_ROLE="claude",
                TELEGRAM_AGENT_CHAT_ID="-1003952617795",
                TELEGRAM_AGENT_TOKEN_FILE=str(token),
            )
            roda = load(
                "peer_coordination_roda",
                "roda-gemma-bot.py",
                RODA_GEMMA_TOKEN_FILE=str(token),
                RODA_GEMMA_ALLOWED_USER_IDS="6417205500",
                RODA_GEMMA_ALLOWED_GROUP_IDS="-1003952617795",
            )
            text = "안티랑 로다는 왜 안해?"
            roles = bot._wake_roles(text)
            self.assertEqual(bot._message_route(text, roles), "coordination")
            self.assertEqual(roda._strip_group_mention(text), "안티랑 왜 안해?")
            # Ordinary group messages belong to Codex, the canonical intake
            # role; Roda only receives explicit/broadcast addresses.
            self.assertIsNone(roda._strip_group_mention("canary"))
            self.assertIsNone(roda._strip_group_mention("/status@edgeai_stk_bot"))
            self.assertEqual(roda._strip_group_mention("/status@sukja_hwpx_helper_bot"), "/status")
            self.assertEqual(bot._message_route("로다야 코덱스 오류 안잡고 머해?"), "external")
            addressed = "안티랑 @sukja_hwpx_helper_bot 현재 상태를 확인하고 합쳐서 알려줘"
            addressed_roles = bot._wake_roles(addressed)
            self.assertEqual(bot._message_route(addressed, addressed_roles), "coordination")
            self.assertEqual(
                roda._strip_group_mention(addressed),
                "안티랑 현재 상태를 확인하고 합쳐서 알려줘",
            )
        finally:
            token_root.cleanup()


if __name__ == "__main__":
    unittest.main()
