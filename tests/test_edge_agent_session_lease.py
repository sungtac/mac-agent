import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_session_lease import SessionLeaseBusy, SessionLeaseManager  # noqa: E402


class SessionLeaseTests(unittest.TestCase):
    def test_same_logical_session_cannot_be_owned_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SessionLeaseManager(temp)
            with manager.acquire("sess-1", "terminal") as lease:
                self.assertEqual(lease.owner, "terminal")
                with self.assertRaises(SessionLeaseBusy):
                    with manager.acquire("sess-1", "telegram"):
                        pass
                self.assertEqual(manager.current_metadata("sess-1")["state"], "active")
            self.assertEqual(manager.current_metadata("sess-1")["state"], "released")

    def test_different_sessions_can_be_owned_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SessionLeaseManager(temp)
            with manager.acquire("sess-a", "terminal"), manager.acquire("sess-b", "telegram"):
                self.assertEqual(manager.current_metadata("sess-a")["owner"], "terminal")
                self.assertEqual(manager.current_metadata("sess-b")["owner"], "telegram")

    def test_lease_inputs_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SessionLeaseManager(temp)
            with self.assertRaises(ValueError):
                with manager.acquire("bad id", "terminal"):
                    pass
            with self.assertRaises(ValueError):
                with manager.acquire("sess-1", "terminal", ttl_seconds=0):
                    pass

    def test_signed_lease_metadata_is_verifiable(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "lease.key"
            key.write_bytes(b"session-lease-key-with-more-than-16-bytes")
            key.chmod(0o600)
            manager = SessionLeaseManager(Path(temp) / "leases", provenance_key_path=key)
            with manager.acquire("signed", "terminal") as lease:
                self.assertTrue(lease.signature)
                self.assertTrue(manager.current_metadata("signed")["signature"])
            released = manager.current_metadata("signed")
            self.assertEqual(released["state"], "released")
            self.assertTrue(released["signature"])


if __name__ == "__main__":
    unittest.main()
