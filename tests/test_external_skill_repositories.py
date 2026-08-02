import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from edge_agent_external_skill_repositories import load_external_skill_repositories  # noqa: E402


class ExternalSkillRepositoryTests(unittest.TestCase):
    def test_codex_bot_uses_the_validated_loader(self):
        source = (Path(__file__).resolve().parents[1] / "bin" / "codex-bot.py").read_text(encoding="utf-8")
        self.assertIn("from edge_agent_external_skill_repositories import load_external_skill_repositories", source)
        self.assertIn("load_external_skill_repositories()", source)

    def test_loads_valid_external_repositories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "hwpx"
            repo.mkdir()
            (repo / "SKILL.md").write_text("---\nname: hwpx\n---\n", encoding="utf-8")
            config = root / "external.json"
            config.write_text(json.dumps({
                "schema": "edge_agent_external_skill_repositories.v1",
                "repositories": {"hwpx-skill": {"path": str(repo), "manifest": "SKILL.md", "status": "external_dependency"}},
            }), encoding="utf-8")
            self.assertEqual(load_external_skill_repositories(config)["hwpx-skill"], repo.resolve())

    def test_missing_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "missing"
            repo.mkdir()
            config = root / "external.json"
            config.write_text(json.dumps({
                "schema": "edge_agent_external_skill_repositories.v1",
                "repositories": {"missing": {"path": str(repo), "manifest": "SKILL.md", "status": "external_dependency"}},
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_external_skill_repositories(config)


if __name__ == "__main__":
    unittest.main()
