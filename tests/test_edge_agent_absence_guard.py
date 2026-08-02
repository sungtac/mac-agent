import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "edge_agent_absence_guard.py"
SPEC = importlib.util.spec_from_file_location("edge_agent_absence_guard", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AbsenceGuardTests(unittest.TestCase):
    def test_bare_capability_absence_claim_is_rejected(self):
        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):
            MODULE.validate_provider_payload({"summary": "Telegram token is not configured"})

    def test_absence_claim_with_discovery_evidence_is_allowed(self):
        result = MODULE.validate_provider_payload({
            "summary": "not found in searched scope",
            "discovery_evidence": {"searched_scopes": ["environment", "/tmp/config"]},
        })
        self.assertTrue(result["validated"])

    def test_local_scan_finds_candidate_names_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / ".edge-agent" / "secrets"
            secrets.mkdir(parents=True)
            secret = secrets / "service.token"
            secret.write_text("must-not-be-read-or-rendered", encoding="utf-8")

            evidence = MODULE.discover_local_sources(home=root)
            encoded = json.dumps(evidence.as_dict(), ensure_ascii=False)

            self.assertTrue(evidence.complete)
            self.assertTrue(any(item.location == str(secret.resolve()) for item in evidence.candidate_sources))
            self.assertNotIn("must-not-be-read-or-rendered", encoded)

    def test_scoped_absence_requires_complete_search(self):
        incomplete = MODULE.DiscoveryEvidence("x", ("environment",), (), (), False, "now")
        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):
            MODULE.scoped_absence_claim("x", incomplete)

        complete = MODULE.DiscoveryEvidence("x", ("environment", "/tmp/config"), ("scan",), (), True, "now")
        claim = MODULE.scoped_absence_claim("x", complete)
        self.assertEqual(claim["status"], "not_found_in_searched_scope")


if __name__ == "__main__":
    unittest.main()
