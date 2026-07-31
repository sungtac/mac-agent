from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_skill_contract_and_state_boundary_are_present():
    assert (ROOT / "docs" / "edge-agent-skill-integration-contract.md").is_file()
    assert (ROOT / "skills" / "edge_agent_skill_paths.py").is_file()


def test_edge_agent_skill_paths_do_not_default_to_openclaw():
    text = (ROOT / "skills" / "edge_agent_skill_paths.py").read_text(encoding="utf-8")
    assert ".openclaw" not in text
    assert "EDGE_AGENT_RUNTIME_ROOT" in text
