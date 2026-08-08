#!/usr/bin/env python3
"""Verify Antigravity identity and tone injection in the Telegram bridge."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN_DIR))
_TOKEN_ROOT = tempfile.TemporaryDirectory()
_TOKEN_FILE = Path(_TOKEN_ROOT.name) / "antigravity.token"
_TOKEN_FILE.write_text("123456:unit-test-token", encoding="utf-8")
_TOKEN_FILE.chmod(0o600)
os.environ["TELEGRAM_AGENT_ROLE"] = "antigravity"
os.environ["TELEGRAM_AGENT_CHAT_ID"] = "-1003952617795"
os.environ["TELEGRAM_AGENT_TOKEN_FILE"] = str(_TOKEN_FILE)
SPEC = importlib.util.spec_from_file_location("telegram_agent_bot", BIN_DIR / "telegram-agent-bot.py")
BOT_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(BOT_MODULE)


class AntigravityIdentityTests(unittest.TestCase):
    def test_identity_and_tone_injection(self):
        identity_text = BOT_MODULE._load_identity_and_tone("antigravity")
        self.assertTrue(identity_text)
        self.assertIn("[영구 아이덴티티 및 톤앤매너 규칙]", identity_text)
        self.assertIn("Antigravity (안티)", identity_text)
        runtime_prompt, _ = BOT_MODULE._runtime_prompt_parts("안티야 테스트 메시지다")
        self.assertIn("[영구 아이덴티티 및 톤앤매너 규칙]", runtime_prompt)
        self.assertIn("Antigravity (안티)", runtime_prompt)

    def test_shared_team_contract_is_injected(self):
        runtime_prompt, _ = BOT_MODULE._runtime_prompt_parts("모두 자기 소개 부탁해")
        self.assertIn("공통 Edge Agent Telegram 팀 계약", runtime_prompt)
        self.assertIn("Claude", runtime_prompt)
        self.assertIn("Codex", runtime_prompt)
        self.assertIn("Roda", runtime_prompt)
        self.assertIn("모두", runtime_prompt)

    def test_headless_policy_blocks_unsandboxed_host_diagnostics(self):
        runtime_prompt, _ = BOT_MODULE._runtime_prompt_parts("현재 다른 봇의 원인을 분석해줘")
        self.assertIn("Antigravity 헤드리스 안전 실행 규칙", runtime_prompt)
        self.assertIn("launchctl", runtime_prompt)
        self.assertIn("unsandboxed", runtime_prompt)

    def test_headless_permission_denial_is_classified(self):
        self.assertTrue(BOT_MODULE._headless_permission_denied(
            "jetski: no output produced — a tool required the unsandboxed permission that headless mode cannot prompt for"
        ))
        self.assertFalse(BOT_MODULE._headless_permission_denied("ordinary provider error"))

    def test_native_session_state_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude-session.json"
            with patch.dict(os.environ, {"TELEGRAM_AGENT_NATIVE_SESSION_FILE": str(path)}):
                self.assertIsNone(BOT_MODULE._load_native_session_id("claude"))
                session_id = "12345678-1234-5678-1234-567812345678"
                BOT_MODULE._persist_native_session_id("claude", session_id)
                self.assertEqual(BOT_MODULE._load_native_session_id("claude"), session_id)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cli_diagnostic_includes_stdout_when_stderr_is_empty(self):
        detail = BOT_MODULE._bounded_cli_diagnostic("provider error on stdout", "")
        self.assertIn("[stdout]", detail)
        self.assertIn("provider error on stdout", detail)

    def test_wake_roles_recognizes_mid_sentence_vocatives_without_bare_mentions(self):
        self.assertEqual(BOT_MODULE._wake_roles("근데 클로드야 이 내용 확인해줘"), {"claude"})
        self.assertEqual(BOT_MODULE._wake_roles("어제 클로드가 이상했어"), set())

    def test_routing_keeps_plain_chat_and_broadcast_separate(self):
        self.assertEqual(BOT_MODULE._message_route("안녕하세요"), "default")
        self.assertEqual(BOT_MODULE._message_route("각자 자기 소개를 해줘"), "broadcast")
        self.assertFalse(BOT_MODULE._needs_task_worktree("각자 자기 소개를 해줘"))
        self.assertTrue(BOT_MODULE._needs_task_worktree("버그를 수정해줘"))

    def test_roda_address_is_not_claimed_by_provider_bots(self):
        self.assertEqual(BOT_MODULE._message_route("로다야 코덱스 오류 안잡고 머해?"), "external")
        self.assertEqual(BOT_MODULE._message_route("@sukja_hwpx_helper_bot 안녕"), "external")

    def test_addressed_text_applies_role_routing(self):
        def update(text):
            message = SimpleNamespace(
                text=text,
                caption=None,
                entities=None,
                caption_entities=None,
                from_user=SimpleNamespace(is_bot=False),
            )
            chat = SimpleNamespace(type=BOT_MODULE.ChatType.GROUP, id=-1003952617795)
            return SimpleNamespace(effective_message=message, effective_chat=chat)

        with patch.object(BOT_MODULE, "ROLE", "claude"):
            self.assertIsNone(BOT_MODULE.addressed_text(update("안녕하세요")))
            self.assertEqual(BOT_MODULE.addressed_text(update("각자 자기소개 해줘")), "각자 자기소개 해줘")
            self.assertIsNone(BOT_MODULE.addressed_text(update("코덱스야 확인해줘")))
            self.assertIsNone(BOT_MODULE.addressed_text(update("로다야 안녕")))
        with patch.object(BOT_MODULE, "ROLE", "codex"):
            self.assertEqual(BOT_MODULE.addressed_text(update("안녕하세요")), "안녕하세요")
            self.assertEqual(BOT_MODULE.addressed_text(update("코덱스야 확인해줘")), "코덱스야 확인해줘")
            self.assertEqual(BOT_MODULE.addressed_text(update("각자 버그를 수정해줘")), "각자 버그를 수정해줘")
        with patch.object(BOT_MODULE, "ROLE", "antigravity"):
            self.assertIsNone(BOT_MODULE.addressed_text(update("안녕하세요")))


if __name__ == "__main__":
    unittest.main()
