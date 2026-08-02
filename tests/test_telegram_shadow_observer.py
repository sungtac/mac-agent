import os
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_observer import (  # noqa: E402
    RUNNING,
    ShadowObserver,
    ShadowObserverConfig,
    extract_ingress_metadata,
    load_config,
)


class FakeUpdate:
    def __init__(self, *, bot_id="bot-a", chat_type="group", chat_id=-100, message_id=7, update_id=70):
        self.update_id = update_id
        self.effective_message = SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            text="비공개 본문",
            caption="비공개 캡션",
            edit_date=None,
            document=SimpleNamespace(file_unique_id="unique-file", file_id="secret-file-id", file_size=12, mime_type="application/pdf"),
            photo=None,
            audio=None,
            voice=None,
            video=None,
            video_note=None,
            animation=None,
            sticker=None,
        )
        self.message = self.effective_message
        self.edited_message = None


class ShadowObserverTests(unittest.TestCase):
    def test_missing_and_false_flags_are_completely_off(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "shadow"
            for value in (None, "", "0", "false", "no", "invalid"):
                env = {"EDGE_AGENT_SHADOW_ROOT": str(root)}
                if value is not None:
                    env["EDGE_AGENT_SHADOW_OBSERVER_ENABLED"] = value
                config = load_config(env)
                observer = ShadowObserver(config)
                self.assertFalse(config.enabled)
                self.assertFalse(observer.start())
                self.assertFalse(root.exists())

    def test_operational_state_root_disables_observer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            config = load_config({
                "EDGE_AGENT_SHADOW_OBSERVER_ENABLED": "1",
                "EDGE_AGENT_SHADOW_ROOT": str(root),
                "EDGE_AGENT_STATE_DIR": str(root),
            })
            self.assertFalse(config.enabled)
            self.assertEqual(config.reason, "operational state root")

    def test_invalid_runtime_settings_disable_observer(self):
        config = load_config({
            "EDGE_AGENT_SHADOW_OBSERVER_ENABLED": "1",
            "EDGE_AGENT_SHADOW_ROOT": "/tmp/shadow-test",
            "EDGE_AGENT_SHADOW_QUEUE_SIZE": "not-an-int",
        })
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "invalid configuration")

    def test_empty_root_is_invalid_and_does_not_select_cwd(self):
        config = load_config({
            "EDGE_AGENT_SHADOW_OBSERVER_ENABLED": "1",
            "EDGE_AGENT_SHADOW_ROOT": "",
        })
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "invalid root")

    def test_enabled_observer_uses_test_root_and_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            config = ShadowObserverConfig(True, Path(temp) / "shadow", queue_size=4, flush_timeout_seconds=0.5)
            observer = ShadowObserver(config)
            self.assertTrue(observer.start())
            update = FakeUpdate()
            self.assertTrue(observer.record_update(update, bot_id="bot-a", bot_role="codex", legacy_target="codex"))
            observer.stop(timeout=1.0)
            self.assertTrue((Path(temp) / "shadow" / "shadow.db").exists())
            self.assertEqual(observer.stats["processed"], 1)

    def test_group_root_is_same_across_bots_and_private_is_scoped(self):
        first = extract_ingress_metadata(FakeUpdate(), bot_id="claude", bot_role="claude")
        second = extract_ingress_metadata(FakeUpdate(), bot_id="codex", bot_role="codex")
        private_a = extract_ingress_metadata(FakeUpdate(chat_type="private", chat_id=9), bot_id="claude", bot_role="claude")
        private_b = extract_ingress_metadata(FakeUpdate(chat_type="private", chat_id=9), bot_id="codex", bot_role="codex")
        self.assertEqual(first["root_task_id"], second["root_task_id"])
        self.assertEqual(first["cross_bot_message_key"], second["cross_bot_message_key"])
        self.assertNotEqual(private_a["root_task_id"], private_b["root_task_id"])

    def test_metadata_does_not_contain_raw_content_or_file_identity(self):
        metadata = extract_ingress_metadata(FakeUpdate(), bot_id="bot-a", bot_role="codex")
        serialized = repr(metadata)
        self.assertNotIn("비공개 본문", serialized)
        self.assertNotIn("비공개 캡션", serialized)
        self.assertNotIn("secret-file-id", serialized)
        self.assertNotIn("unique-file", serialized)
        self.assertEqual(metadata["body_hash_status"], "UNKNOWN")
        self.assertIsNone(metadata["body_hash"])
        self.assertEqual(metadata["attachment_metadata"][0]["hash_status"], "UNKNOWN")
        self.assertIsNone(metadata["attachment_metadata"][0]["content_hash"])

    def test_hmac_body_fingerprint_is_allowed(self):
        metadata = extract_ingress_metadata(FakeUpdate(), bot_id="bot-a", bot_role="codex", hmac_key=b"test-key")
        self.assertEqual(metadata["body_hash_status"], "HMAC-SHA256")
        self.assertIsNotNone(metadata["body_hash"])

    def test_unsupported_update_is_ignored(self):
        update = SimpleNamespace(update_id=1, callback_query=SimpleNamespace(id="callback"), effective_message=None)
        self.assertIsNone(extract_ingress_metadata(update, bot_id="bot-a", bot_role="codex"))

    def test_queue_full_is_fail_open(self):
        config = ShadowObserverConfig(True, Path(tempfile.gettempdir()) / "not-used", queue_size=1)
        observer = ShadowObserver(config)
        observer._queue = queue.Queue(maxsize=1)
        observer._set_state(RUNNING)
        metadata = {"root_task_id": "root", "revision_id": "rev"}
        self.assertTrue(observer.enqueue(metadata))
        self.assertFalse(observer.enqueue(metadata))
        self.assertEqual(observer.stats["dropped"], 1)

    def test_record_update_before_start_is_noop(self):
        observer = ShadowObserver(ShadowObserverConfig(True, Path(tempfile.gettempdir()) / "not-used"))
        self.assertFalse(observer.record_update(FakeUpdate(), bot_id="bot", bot_role="codex"))

    def test_initialization_failure_is_fail_open(self):
        config = ShadowObserverConfig(True, Path(tempfile.gettempdir()) / "not-used")
        observer = ShadowObserver(config)
        observer.config = ShadowObserverConfig(True, Path("/dev/null/impossible"))
        self.assertFalse(observer.start())
        self.assertFalse(observer.active)
        self.assertEqual(observer.config.root, Path("/dev/null/impossible"))


if __name__ == "__main__":
    unittest.main()
