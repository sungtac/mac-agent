import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "read_provider_usage_snapshot", ROOT / "bin" / "read-provider-usage-snapshot.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def snapshot(observed_at):
    return {
        "schema": "edge_agent_provider_usage_snapshot.v1",
        "observed_at": observed_at,
        "providers": {"claude": {"windows": {"5h": {"left_pct": 50}}}},
    }


class ReadProviderUsageSnapshotTests(unittest.TestCase):
    def test_missing_and_invalid_are_not_fresh(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing.jsonl"
            self.assertEqual(MODULE.read_latest(path, 3600)["status"], "missing")
            path.write_text("not-json\n")
            self.assertEqual(MODULE.read_latest(path, 3600)["status"], "invalid")

    def test_latest_snapshot_is_fresh_or_stale_by_age(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshots.jsonl"
            now = datetime.now(timezone.utc)
            path.write_text(json.dumps(snapshot(now.isoformat())) + "\n")
            result = MODULE.read_latest(path, 3600)
            self.assertEqual(result["status"], "fresh")
            old = now - timedelta(hours=2)
            path.write_text(json.dumps(snapshot(old.isoformat())) + "\n")
            self.assertEqual(MODULE.read_latest(path, 3600)["status"], "stale")


if __name__ == "__main__":
    unittest.main()
