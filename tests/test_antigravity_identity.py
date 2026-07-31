#!/usr/bin/env python3
"""Verify Antigravity identity and tone injection in the Telegram bridge."""

import importlib.util
import os
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
