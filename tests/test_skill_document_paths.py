import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillDocumentPathTests(unittest.TestCase):
    def test_skill_commands_point_at_tracked_implementations(self):
        checks = {
            ROOT / "skills" / "calendar" / "SKILL.md": (
                "scripts/google_calendar.py",
                ROOT / "skills" / "calendar" / "google_calendar.py",
            ),
            ROOT / "skills" / "product_research" / "SKILL.md": (
                "scripts/product_researcher.py",
                ROOT / "skills" / "product_research" / "product_researcher.py",
            ),
            ROOT / "skills" / "hermes_runtime" / "SKILL.md": (
                "scripts/hermes_lifecycle_gate.py",
                ROOT / "skills" / "hermes_runtime" / "hermes_lifecycle_gate.py",
            ),
        }
        for document, (stale_path, implementation) in checks.items():
            self.assertNotIn(stale_path, document.read_text())
            self.assertTrue(implementation.is_file(), implementation)


if __name__ == "__main__":
    unittest.main()
