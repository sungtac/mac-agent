#!/usr/bin/env python3
"""Verify Antigravity identity and tone injection in the Telegram bridge."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN_DIR))
os.environ.setdefault("TELEGRAM_AGENT_ROLE", "antigravity")
os.environ.setdefault("TELEGRAM_AGENT_CHAT_ID", "-1003952617795")
os.environ.setdefault("TELEGRAM_AGENT_TOKEN_FILE", str(Path.home() / ".config" / "agent-telegram" / "antigravity.token"))
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


if __name__ == "__main__":
    unittest.main()
