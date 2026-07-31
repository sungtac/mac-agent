import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "edge_agent_capability_preflight.py"
SPEC = importlib.util.spec_from_file_location("edge_agent_capability_preflight", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CapabilityPreflightTests(unittest.TestCase):
    def test_collect_is_read_only_and_classifies_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            with patch.object(MODULE, "_run_quiet", return_value=(0, "") ):
                observations = MODULE.collect(workdir)
        by_name = {item.capability: item for item in observations}
        self.assertEqual(by_name["workspace"].state, "available")
        self.assertEqual(by_name["repository_remote"].state, "available")

    def test_prompt_does_not_include_probe_output(self):
        with patch.object(MODULE, "_run_quiet", return_value=(0, "secret=must-not-appear")):
            rendered = MODULE.render_prompt("/tmp")
        self.assertNotIn("must-not-appear", rendered)
        self.assertIn("unknown means verify further", rendered)

    def test_json_is_machine_readable(self):
        payload = json.loads(MODULE.json.dumps([MODULE.asdict(item) for item in MODULE.collect(None)]))
        self.assertIsInstance(payload, list)
        self.assertTrue(all("capability" in item and "state" in item for item in payload))


if __name__ == "__main__":
    unittest.main()
