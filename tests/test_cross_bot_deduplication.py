import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_task_identity import cross_bot_message_key, telegram_update_key  # noqa: E402


class CrossBotDeduplicationTests(unittest.TestCase):
    def test_group_cross_bot_key_is_equal(self):
        first = cross_bot_message_key("telegram", "group", shared_chat_id=-1001, message_id=8, bot_id="claude")
        second = cross_bot_message_key("telegram", "group", shared_chat_id=-1001, message_id=8, bot_id="antigravity")
        self.assertEqual(first, second)

    def test_private_cross_bot_key_is_not_equal(self):
        first = cross_bot_message_key("telegram", "private", bot_id="claude", chat_id=7, message_id=8)
        second = cross_bot_message_key("telegram", "private", bot_id="codex", chat_id=7, message_id=8)
        self.assertNotEqual(first, second)

    def test_update_key_is_bot_specific(self):
        first = telegram_update_key("telegram", bot_id="claude", chat_id=-1001, update_id=88)
        second = telegram_update_key("telegram", bot_id="codex", chat_id=-1001, update_id=88)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
