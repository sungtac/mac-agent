import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
SPEC = importlib.util.spec_from_file_location(
    "discord_bot_common", ROOT / "bin" / "discord_bot_common.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class AtomicJsonTests(unittest.TestCase):
    def test_atomic_write_json_creates_valid_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "pending" / "job.json"
            MODULE.atomic_write_json(target, {"type": "test", "params": {"x": 1}})
            self.assertEqual(json.loads(target.read_text()), {"type": "test", "params": {"x": 1}})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(target.parent.glob(".*.job.json.*")), [])


if __name__ == "__main__":
    unittest.main()
