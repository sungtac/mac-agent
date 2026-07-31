from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SkillIntegrationContractTests(unittest.TestCase):
    def test_skill_contract_and_state_boundary_are_present(self):
        self.assertTrue((ROOT / "docs" / "edge-agent-skill-integration-contract.md").is_file())
        self.assertTrue((ROOT / "skills" / "edge_agent_skill_paths.py").is_file())


    def test_edge_agent_skill_paths_do_not_default_to_openclaw(self):
        text = (ROOT / "skills" / "edge_agent_skill_paths.py").read_text(encoding="utf-8")
        self.assertNotIn(".openclaw", text)
        self.assertIn("EDGE_AGENT_RUNTIME_ROOT", text)

    def test_migrated_skill_manifests_are_present(self):
        expected = {
            "command-registry": ROOT / "skills" / "command-registry" / "SKILL.md",
            "harness-memory": ROOT / "skills" / "harness-memory" / "SKILL.md",
            "hermes_runtime": ROOT / "skills" / "hermes_runtime" / "SKILL.md",
            "quota_resume": ROOT / "skills" / "quota_resume" / "SKILL.md",
            "product_research": ROOT / "skills" / "product_research" / "SKILL.md",
            "calendar": ROOT / "skills" / "calendar" / "SKILL.md",
            "roda_public_search": ROOT / "skills" / "roda_public_search" / "SKILL.md",
        }
        for name, path in expected.items():
            self.assertTrue(path.is_file(), name)

    def test_helper_code_has_no_legacy_workspace_dependency(self):
        for path in (ROOT / "skills").rglob("*.py"):
            self.assertNotIn(".openclaw/workspace", path.read_text(encoding="utf-8"), str(path))


if __name__ == "__main__":
    unittest.main()
