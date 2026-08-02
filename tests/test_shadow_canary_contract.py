from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_maintenance import ShadowCanaryConfig  # noqa: E402


class ShadowCanaryContractTests(unittest.TestCase):
    def test_antigravity_is_first_candidate(self):
        self.assertEqual(ShadowCanaryConfig().provider_role, "antigravity")

    def test_other_provider_flags_are_required_off(self):
        self.assertTrue(ShadowCanaryConfig().other_provider_flags_off)

    def test_telegram_output_is_forbidden(self):
        self.assertTrue(ShadowCanaryConfig(telegram_output_enabled=True).validate())

    def test_enabled_canary_requires_root(self):
        self.assertTrue(ShadowCanaryConfig(enabled=True, key_path=Path("key")).validate())

    def test_enabled_canary_with_required_inputs_is_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            config = ShadowCanaryConfig(enabled=True, root=Path(temp) / "root", key_path=Path(temp) / "key")
            self.assertEqual(config.validate(), [])


if __name__ == "__main__":
    unittest.main()
