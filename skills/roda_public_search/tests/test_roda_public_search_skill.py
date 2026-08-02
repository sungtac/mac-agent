import unittest
from pathlib import Path


class RodaPublicSearchSkillTests(unittest.TestCase):
    def test_manifest_contains_public_search_safety_contract(self):
        path = Path(__file__).resolve().parents[1] / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        for phrase in (
            "public sources only",
            "private-person dossier",
            "same-name",
            "No automatic send",
            "source URL",
            "Untrusted metadata rule",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
