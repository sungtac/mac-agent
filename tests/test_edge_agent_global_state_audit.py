import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_edge_agent_global_state", ROOT / "bin" / "audit-edge-agent-global-state.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class GlobalStateAuditTests(unittest.TestCase):
    def test_report_is_read_only_and_has_safe_contract(self):
        report = MODULE.collect()
        self.assertTrue(report["read_only"])
        self.assertEqual(report["schema"], "edge_agent_global_state_inventory.v1")
        self.assertGreaterEqual(len(report["items"]), 8)
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("token", encoded.lower())

    def test_entry_reports_missing_path_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "does-not-exist"
            item = MODULE._entry(path, "test", "test", "test")
            self.assertFalse(item["exists"])
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
