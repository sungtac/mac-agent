#!/usr/bin/env python3
"""Static contract for the Discord/Telegram shared workspace boundary."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EDGE_WORKTREE = Path.home() / ".edge-agent-worktrees" / "telegram-bootstrap"


class SharedChannelEnvironmentTests(unittest.TestCase):
    def test_discord_uses_edge_agent_worktree_as_free_chat_cwd(self):
        source = (ROOT / "bin" / "discord-bot.py").read_text(encoding="utf-8")
        self.assertIn("EDGE_AGENT_WORKSPACE", source)
        self.assertIn("FREE_CHAT_CWD = EDGE_AGENT_WORKSPACE", source)
        self.assertIn('"EDGE_AGENT_HOME": str(EDGE_AGENT_HOME)', source)
        self.assertIn('"EDGE_AGENT_WORKSPACE": str(EDGE_AGENT_WORKSPACE)', source)
        self.assertNotIn('Path.home() / ".openclaw"', source)

    def test_telegram_uses_the_edge_agent_worktree_by_default(self):
        source = (ROOT / "bin" / "telegram-agent-bot.py").read_text(encoding="utf-8")
        self.assertIn('TELEGRAM_AGENT_WORKSPACE', source)
        self.assertIn('HOME / ".edge-agent-worktrees" / "telegram-bootstrap"', source)
        self.assertEqual(EDGE_WORKTREE, Path.home() / ".edge-agent-worktrees" / "telegram-bootstrap")


if __name__ == "__main__":
    unittest.main()
