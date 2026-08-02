import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from edge_agent_skill_catalog import catalog_skill_ids, load_catalog, validate_catalog_covers_manifests  # noqa: E402


class SkillCatalogTests(unittest.TestCase):
    def test_catalog_is_valid_and_covers_every_repository_manifest(self):
        catalog = load_catalog()
        self.assertEqual(len(catalog["skills"]), 9)
        self.assertEqual(validate_catalog_covers_manifests(), ())
        self.assertEqual(len(catalog_skill_ids()), 9)

    def test_catalog_paths_stay_inside_skills_root(self):
        for entry in load_catalog()["skills"]:
            self.assertTrue(entry["manifest"].endswith("/SKILL.md"))
            self.assertFalse(Path(entry["manifest"]).is_absolute())
            for test_path in entry.get("tests", []):
                repo_root = Path(__file__).resolve().parents[1]
                self.assertTrue((repo_root / test_path).exists() or (repo_root / "skills" / test_path).exists(), test_path)


if __name__ == "__main__":
    unittest.main()
