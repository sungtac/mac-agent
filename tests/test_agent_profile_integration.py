#!/usr/bin/env python3
"""Provider integration checks for the shared profile contract."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AgentProfileIntegrationTests(unittest.TestCase):
    def test_provider_adapters_use_the_shared_profile_loader(self):
        telegram = (ROOT / "bin" / "telegram-agent-bot.py").read_text(encoding="utf-8")
        discord_common = (ROOT / "bin" / "discord_bot_common.py").read_text(encoding="utf-8")
        self.assertIn("from agent_profile import render_agent_profile", telegram)
        self.assertIn("from agent_profile import render_agent_profile", discord_common)
        self.assertIn('render_agent_profile("claude", "coordinator")', discord_common)
        self.assertIn('render_agent_profile("codex", "implementer")', discord_common)

    def test_provider_identity_files_exist_for_legacy_loaders(self):
        for role in ("claude", "codex", "antigravity"):
            self.assertTrue((ROOT / "config" / f"{role}-identity-and-tone.md").is_file(), role)


if __name__ == "__main__":
    unittest.main()
