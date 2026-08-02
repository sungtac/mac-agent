from pathlib import Path
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_claim_store import (  # noqa: E402
    ACTIVE,
    CLAIMED,
    COMPLETED,
    EXPIRED,
    FENCING_REJECTED,
    RECOVERY_CANDIDATE,
    ClaimStore,
)


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


class ClaimLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = ClaimStore(Path(self.tempdir.name) / "claim.db", ttl_seconds=10, clock=self.clock)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_first_claim_is_acquired(self):
        result = self.store.claim("message", "root", "owner-a")
        self.assertTrue(result.acquired)
        self.assertEqual(result.status, CLAIMED)
        self.assertEqual(result.fencing_token, 1)

    def test_valid_lease_blocks_other_owner(self):
        self.store.claim("message", "root", "owner-a")
        result = self.store.claim("message", "root", "owner-b")
        self.assertFalse(result.acquired)
        self.assertEqual(result.status, ACTIVE)

    def test_same_owner_duplicate_is_idempotent(self):
        first = self.store.claim("message", "root", "owner-a")
        second = self.store.claim("message", "root", "owner-a")
        self.assertTrue(second.acquired)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.fencing_token, second.fencing_token)

    def test_expired_claim_is_reacquired_with_incremented_fencing_token(self):
        self.store.claim("message", "root", "owner-a")
        self.clock.set(111)
        result = self.store.claim("message", "root", "owner-b")
        self.assertTrue(result.acquired)
        self.assertEqual(result.fencing_token, 2)
        self.assertEqual(result.reason, "lease_expired")

    def test_old_fencing_token_is_rejected(self):
        self.store.claim("message", "root", "owner-a")
        self.clock.set(111)
        current = self.store.claim("message", "root", "owner-b")
        rejected = self.store.complete("message", "owner-a", 1)
        self.assertFalse(rejected.acquired)
        self.assertEqual(rejected.status, FENCING_REJECTED)
        self.assertTrue(self.store.validate_fencing_token("message", current.fencing_token))

    def test_heartbeat_extends_lease(self):
        claimed = self.store.claim("message", "root", "owner-a")
        self.clock.set(105)
        result = self.store.heartbeat("message", "owner-a", claimed.fencing_token)
        self.assertTrue(result.acquired)
        self.assertEqual(result.status, ACTIVE)
        self.assertEqual(self.store.get("message").lease_expires_at, 115)

    def test_expired_heartbeat_is_not_accepted(self):
        claimed = self.store.claim("message", "root", "owner-a")
        self.clock.set(111)
        result = self.store.heartbeat("message", "owner-a", claimed.fencing_token)
        self.assertFalse(result.acquired)
        self.assertEqual(result.status, EXPIRED)

    def test_clock_anomaly_becomes_recovery_candidate(self):
        store = ClaimStore(Path(self.tempdir.name) / "clock.db", ttl_seconds=3, clock=self.clock, max_clock_jump_seconds=5)
        store.claim("message", "root", "owner-a")
        self.clock.set(20)
        result = store.claim("message", "root", "owner-b")
        self.assertFalse(result.acquired)
        self.assertEqual(result.status, RECOVERY_CANDIDATE)

    def test_concurrent_claims_have_one_winner(self):
        results = []
        barrier = threading.Barrier(8)

        def worker(index):
            barrier.wait()
            result = self.store.claim("same-message", "root", f"owner-{index}")
            results.append(result)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for item in results if item.acquired and not item.duplicate), 1)
        self.assertEqual(sum(1 for item in results if item.acquired), 1)

    def test_completion_is_terminal(self):
        claimed = self.store.claim("message", "root", "owner-a")
        result = self.store.complete("message", "owner-a", claimed.fencing_token)
        self.assertEqual(result.status, COMPLETED)
        duplicate = self.store.claim("message", "root", "owner-a")
        self.assertFalse(duplicate.acquired)
        self.assertEqual(duplicate.status, COMPLETED)


if __name__ == "__main__":
    unittest.main()
