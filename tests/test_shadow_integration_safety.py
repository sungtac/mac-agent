import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

from edge_agent_shadow_event_store import ShadowEventError, ShadowEventStore  # noqa: E402


class ShadowIntegrationSafetyTests(unittest.TestCase):
    def _load_bot(self, enabled: str):
        token_root = tempfile.TemporaryDirectory()
        token = Path(token_root.name) / "claude.token"
        token.write_text("123456:unit-test-token", encoding="utf-8")
        token.chmod(0o600)
        old = {key: os.environ.get(key) for key in (
            "TELEGRAM_AGENT_ROLE", "TELEGRAM_AGENT_CHAT_ID", "TELEGRAM_AGENT_TOKEN_FILE",
            "EDGE_AGENT_SHADOW_OBSERVER_ENABLED", "EDGE_AGENT_SHADOW_ROOT",
        )}
        os.environ.update({
            "TELEGRAM_AGENT_ROLE": "claude",
            "TELEGRAM_AGENT_CHAT_ID": "-1003952617795",
            "TELEGRAM_AGENT_TOKEN_FILE": str(token),
            "EDGE_AGENT_SHADOW_OBSERVER_ENABLED": enabled,
        })
        sys.modules.pop("edge_agent_shadow_observer", None)
        spec = importlib.util.spec_from_file_location("shadow_safety_bot", BIN / "telegram-agent-bot.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        token_root.cleanup()
        return module

    def test_flag_off_does_not_import_observer(self):
        bot = self._load_bot("0")
        self.assertIsNone(bot.SHADOW_OBSERVER)
        self.assertNotIn("edge_agent_shadow_observer", sys.modules)

    def test_import_failure_is_fail_open(self):
        bot = self._load_bot("1")
        with patch.object(bot.importlib, "import_module", side_effect=ImportError("synthetic")):
            self.assertIsNone(bot._get_shadow_observer())
        self.assertIsNone(bot.SHADOW_OBSERVER)

    def test_protected_root_and_nested_root_are_rejected(self):
        from edge_agent_shadow_observer import load_config

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            child = state / "shadow"
            self.assertFalse(load_config({
                "EDGE_AGENT_SHADOW_OBSERVER_ENABLED": "1",
                "EDGE_AGENT_SHADOW_ROOT": str(state),
                "EDGE_AGENT_STATE_DIR": str(state),
            }).enabled)
            self.assertFalse(load_config({
                "EDGE_AGENT_SHADOW_OBSERVER_ENABLED": "1",
                "EDGE_AGENT_SHADOW_ROOT": str(child),
                "EDGE_AGENT_STATE_DIR": str(state),
            }).enabled)

    def test_shadow_files_are_private_and_existing_wide_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "shadow"
            store = ShadowEventStore(root)
            event = {
                "event_id": "private-event",
                "event_type": "ingress_observed",
                "root_task_id": "root",
                "body_hash": None,
            }
            store.append(event)
            for path in (root, store.database_path, store.event_log_path, store.manifest_path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path == root else 0o600)
            root.chmod(0o777)
            store.database_path.chmod(0o666)
            store.manifest_path.chmod(0o644)
            store.event_log_path.chmod(0o644)
            with self.assertRaisesRegex(ShadowEventError, "permissions must be exactly 0700"):
                ShadowEventStore(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o777)
            root.chmod(0o700)
            ShadowEventStore(root)
            for path in (store.database_path, store.event_log_path, store.manifest_path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_managed_shadow_symlinks_are_rejected(self):
        for managed_name in ("shadow.db", "shadow-events.jsonl", "manifest.yaml", "rotation.lock"):
            with self.subTest(managed_name=managed_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "shadow"
                root.mkdir(mode=0o700)
                outside = Path(temp) / f"outside-{managed_name}"
                outside.write_text("outside", encoding="utf-8")
                (root / managed_name).symlink_to(outside)
                with self.assertRaises(ShadowEventError):
                    ShadowEventStore(root)


if __name__ == "__main__":
    unittest.main()
