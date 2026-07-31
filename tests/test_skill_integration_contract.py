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


if __name__ == "__main__":
    unittest.main()
