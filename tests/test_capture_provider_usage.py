import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "capture_provider_usage", ROOT / "bin" / "capture-provider-usage.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class CaptureProviderUsageTests(unittest.TestCase):
    def test_sanitizes_provider_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            coach = Path(temp) / "coach"
            coach.write_text(
                '#!/bin/sh\n'
                'printf \'%s\' \'{"providers":{"claude":{"windows":{"5h":{"left_pct":42,"reset_at":"2030-01-01T00:00:00Z","secret":"drop"}}},"codex":{"windows":{"7d":{"left_pct":88}}}}}\'\n'
            )
            coach.chmod(coach.stat().st_mode | stat.S_IXUSR)
            snapshot = MODULE.read_usage(str(coach))
            self.assertEqual(snapshot["providers"]["claude"]["windows"]["5h"]["left_pct"], 42)
            self.assertNotIn("secret", json.dumps(snapshot))

    def test_append_is_atomic_and_jsonl(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "snapshots.jsonl"
            snapshot = {"schema": "test", "providers": {"claude": {"windows": {"5h": {"left_pct": 50}}}}}
            MODULE.append_snapshot(snapshot, output)
            MODULE.append_snapshot(snapshot, output)
            self.assertEqual(len(output.read_text().splitlines()), 2)
            self.assertTrue(all(json.loads(line)["schema"] == "test" for line in output.read_text().splitlines()))


if __name__ == "__main__":
    unittest.main()
