from pathlib import Path
import os
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_keyring import HMACKeyring, ShadowKeyError, create_test_key  # noqa: E402
from edge_agent_shadow_observer import extract_ingress_metadata, load_config  # noqa: E402


class ShadowKeyRotationTests(unittest.TestCase):
    def test_valid_key_fingerprint_has_key_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path = create_test_key(Path(temp) / "key")
            keyring = HMACKeyring(path)
            result = keyring.fingerprint("short message")
            self.assertEqual(result["body_hash_status"], "HMAC-SHA256")
            self.assertEqual(result["body_hmac_key_id"], keyring.key_id)

    def test_key_bytes_never_appear_in_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = create_test_key(Path(temp) / "key", value=b"a" * 32)
            keyring = HMACKeyring(path)
            self.assertNotIn("a" * 32, str(keyring.metadata))
            self.assertNotIn((b"a" * 32).decode("ascii"), str(keyring.metadata))

    def test_missing_key_returns_unknown_only_at_observer_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            keyring = None
            with self.assertRaises(ShadowKeyError):
                HMACKeyring(Path(temp) / "missing")
            self.assertIsNone(keyring)

    def test_simple_sha256_fallback_is_not_used(self):
        result = {"body_hash": None, "body_hash_status": "UNKNOWN"}
        self.assertIsNone(result["body_hash"])

    def test_rotation_changes_key_id(self):
        with tempfile.TemporaryDirectory() as temp:
            keyring = HMACKeyring(create_test_key(Path(temp) / "key", value=b"a" * 32))
            old_id = keyring.key_id
            new = keyring.rotate(b"b" * 32, now=100)
            self.assertNotEqual(old_id, new.key_id)
            self.assertEqual(keyring.key_id, new.key_id)

    def test_rotation_does_not_change_task_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = create_test_key(Path(temp) / "key", value=b"a" * 32)
            keyring = HMACKeyring(path)
            before = keyring.key_id
            immutable = {"platform": "telegram", "chat_scope": "group", "shared_chat_id": "1", "message_id": 2}
            from edge_agent_task_identity import root_task_id
            root_before = root_task_id(**immutable)
            keyring.rotate(b"b" * 32, now=200)
            self.assertEqual(root_before, root_task_id(**immutable))
            self.assertNotEqual(before, keyring.key_id)

    def test_rotation_does_not_change_ingress_revision_basis(self):
        from edge_agent_shadow_observer import extract_ingress_metadata

        class Chat:
            id = 42
            type = "group"

        class Message:
            chat = Chat()
            message_id = 7
            text = "same"
            edit_date = None

        class Update:
            update_id = 8
            effective_message = Message()
            edited_message = None
            message = Message()

        first = extract_ingress_metadata(Update(), bot_id="a", bot_role="a", hmac_key=b"a" * 32, hmac_key_id="old")
        second = extract_ingress_metadata(Update(), bot_id="a", bot_role="a", hmac_key=b"b" * 32, hmac_key_id="new")
        self.assertEqual(first["revision_id"], second["revision_id"])
        self.assertNotEqual(first["body_hash"], second["body_hash"])

    def test_world_readable_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = create_test_key(Path(temp) / "key")
            os.chmod(path, 0o644)
            with self.assertRaises(ShadowKeyError):
                HMACKeyring(path)

    def test_broad_key_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "keys"
            parent.mkdir(mode=0o755)
            path = parent / "key"
            path.write_bytes(b"a" * 32)
            path.chmod(0o600)
            with self.assertRaises(ShadowKeyError):
                HMACKeyring(path)

    def test_symlink_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            real = create_test_key(Path(temp) / "real")
            link = Path(temp) / "key"
            link.symlink_to(real)
            with self.assertRaises(ShadowKeyError):
                HMACKeyring(link)

    def test_key_metadata_mismatch_is_rejected_after_interrupted_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = create_test_key(Path(temp) / "key", value=b"a" * 32)
            metadata = path.with_name(path.name + ".metadata.json")
            metadata.write_text(json.dumps({"key_id": "hmac-wrong"}), encoding="utf-8")
            os.chmod(metadata, 0o600)
            with self.assertRaises(ShadowKeyError):
                HMACKeyring(path)

    def test_invalid_existing_key_disables_observer(self):
        with tempfile.TemporaryDirectory() as temp:
            path = create_test_key(Path(temp) / "key")
            os.chmod(path, 0o644)
            config = load_config({
                "EDGE_AGENT_SHADOW_OBSERVER_ENABLED": "1",
                "EDGE_AGENT_SHADOW_ROOT": str(Path(temp) / "shadow"),
                "EDGE_AGENT_SHADOW_BODY_HMAC_KEY_FILE": str(path),
            })
            self.assertFalse(config.enabled)


if __name__ == "__main__":
    unittest.main()
