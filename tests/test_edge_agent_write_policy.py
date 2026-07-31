#!/usr/bin/env python3
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "edge-agent-write-policy.py"
SPEC = importlib.util.spec_from_file_location("edge_agent_write_policy", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class EdgeAgentWritePolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.workspace = self.root / "workspace"
        self.team_os = self.workspace / "team_os"
        self.state = self.workspace / "state"
        self.team_os.mkdir(parents=True)
        self.state.mkdir()
        self.manifest = self.root / "boundary.json"
        self.manifest.write_text(json.dumps({
            "mode": "deny_protected_paths_dry_run",
            "legacy_shared_workspace": str(self.workspace),
            "protected_roots": [str(self.team_os), str(self.state)],
        }), encoding="utf-8")
        self.policy = MODULE.EdgeAgentWritePolicy(self.manifest)

    def test_protected_roots_are_denied(self):
        decision = self.policy.classify(self.team_os / "execution" / "approval.py")
        self.assertEqual(decision.classification, "protected")
        self.assertFalse(decision.allowed_by_policy)

    def test_non_protected_legacy_workspace_path_is_allowed_in_dry_run(self):
        decision = self.policy.classify(self.workspace / "project" / "notes.md")
        self.assertEqual(decision.classification, "legacy_workspace_allowed")
        self.assertTrue(decision.allowed_by_policy)

    def test_outside_path_requires_explicit_decision(self):
        decision = self.policy.classify(self.root / "other" / "file.txt")
        self.assertEqual(decision.classification, "outside_legacy_workspace")
        self.assertFalse(decision.allowed_by_policy)


if __name__ == "__main__":
    unittest.main()
