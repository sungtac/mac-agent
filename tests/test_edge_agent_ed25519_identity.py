import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_agent_message import build_message, verify_message  # noqa: E402
from edge_agent_ed25519_identity import Ed25519Identity, Ed25519IdentityError  # noqa: E402
from edge_agent_message_bus import MessageBus  # noqa: E402


class Ed25519IdentityTests(unittest.TestCase):
    def test_generate_sign_verify_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Ed25519Identity.generate(directory, agent_id="codex", key_id="ed25519-codex-v1")
            payload = b"canonical peer payload"
            signature = identity.sign(payload)
            self.assertTrue(identity.verify(payload, signature))
            self.assertFalse(identity.verify(b"tampered payload", signature))
            public_only = Ed25519Identity.from_paths(
                agent_id="codex",
                key_id="ed25519-codex-v1",
                public_key_path=identity.public_key_path,
            )
            self.assertFalse(public_only.has_private_key)
            with self.assertRaises(Ed25519IdentityError):
                public_only.sign(payload)

    def test_agent_message_uses_ed25519_identity_when_opted_in(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Ed25519Identity.generate(directory, agent_id="codex", key_id="ed25519-codex-v1")
            message = build_message(
                session_id="session-ed25519",
                task_id="task-ed25519",
                from_role="codex",
                to=("claude",),
                purpose="identity-check",
                summary="signed with asymmetric identity",
                source_event_id="event-ed25519",
                key_id=identity.key_id,
                signing_key=identity,
            )
            self.assertTrue(verify_message(message, identity, expected_key_id=identity.key_id))
            forged = type(message)(**{**message.to_dict(), "summary": "forged"})
            self.assertFalse(identity.verify(forged.canonical_bytes(), message.signature))

    def test_message_bus_accepts_opt_in_ed25519_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Ed25519Identity.generate(Path(directory) / "keys", agent_id="codex", key_id="ed25519-codex-v1")
            message = build_message(
                session_id="session-ed25519-bus",
                task_id="task-ed25519-bus",
                from_role="codex",
                to=("claude",),
                purpose="identity-check",
                summary="bus verifies asymmetric identity",
                source_event_id="event-ed25519-bus",
                key_id=identity.key_id,
                signing_key=identity,
            )
            bus = MessageBus(Path(directory) / "bus")
            bus.create_session(message.session_id)
            published = bus.publish(message, verification_key=identity)
            self.assertTrue(published["message_id"])

    def test_private_key_permissions_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Ed25519Identity.generate(directory, agent_id="codex", key_id="ed25519-codex-v1")
            identity.private_key_path.chmod(0o644)
            with self.assertRaises(Ed25519IdentityError):
                identity.sign(b"blocked")

    def test_key_symlinks_are_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Ed25519Identity.generate(directory, agent_id="codex", key_id="ed25519-codex-v1")
            alias = Path(directory) / "alias.pem"
            alias.symlink_to(identity.public_key_path)
            with self.assertRaises(Ed25519IdentityError):
                Ed25519Identity.from_paths(
                    agent_id="codex",
                    key_id="ed25519-codex-v1",
                    public_key_path=alias,
                )


if __name__ == "__main__":
    unittest.main()
