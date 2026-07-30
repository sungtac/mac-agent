#!/usr/bin/env python3
"""Static contract for the Discord/Telegram shared workspace boundary."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_ROOT = Path.home() / ".openclaw" / "workspace"


class SharedChannelEnvironmentTests(unittest.TestCase):
    def test_discord_uses_openclaw_workspace_as_free_chat_cwd(self):
        source = (ROOT / "bin" / "discord-bot.py").read_text(encoding="utf-8")
        self.assertIn("OPENCLAW_WORKSPACE", source)
        self.assertIn("FREE_CHAT_CWD = OPENCLAW_WORKSPACE", source)
        self.assertIn('"OPENCLAW_HOME": str(OPENCLAW_HOME)', source)
        self.assertIn('"OPENCLAW_WORKSPACE": str(OPENCLAW_WORKSPACE)', source)

    def test_telegram_honors_the_same_workspace_environment_variable(self):
        config = (TELEGRAM_ROOT / "sukja_telegram" / "config.py").read_text(encoding="utf-8")
        runner = (TELEGRAM_ROOT / "sukja_telegram" / "telegram_runner.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("OPENCLAW_WORKSPACE"', config)
        self.assertIn('os.environ.get("OPENCLAW_WORKSPACE"', runner)


if __name__ == "__main__":
    unittest.main()
