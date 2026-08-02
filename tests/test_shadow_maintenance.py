from pathlib import Path
import os
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_shadow_event_store import ShadowEventStore  # noqa: E402
from edge_agent_shadow_maintenance import (  # noqa: E402
    ShadowCanaryConfig,
    ShadowMaintenance,
    ShadowMaintenanceConfig,
    ShadowMaintenanceError,
)


class ShadowMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ShadowEventStore(Path(self.temp.name) / "shadow")
        self.maintenance = ShadowMaintenance(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_status_command_is_non_destructive(self):
        result = self.maintenance.command("status")
        self.assertTrue(result["enabled"])
        self.assertTrue(self.store.database_path.exists())

    def test_verify_command_returns_health(self):
        result = self.maintenance.command("verify")
        self.assertIn("sqlite_bytes", result)

    def test_unknown_command_is_rejected(self):
        with self.assertRaises(ShadowMaintenanceError):
            self.maintenance.command("drop-everything")

    def test_purge_defaults_to_dry_run(self):
        result = self.maintenance.command("purge-all-dry-run")
        self.assertTrue(result["dry_run"])
        self.assertTrue(self.store.database_path.exists())

    def test_purge_execute_requires_observer_off(self):
        with self.assertRaises(ShadowMaintenanceError):
            self.maintenance.purge_all(dry_run=False, feature_enabled=True)

    def test_purge_execute_removes_shadow_files_only(self):
        marker = self.store.root / "marker"
        marker.write_text("x", encoding="utf-8")
        self.maintenance.purge_all(dry_run=False)
        self.assertFalse(marker.exists())
        self.assertTrue(self.store.root.exists())

    def test_symlink_root_is_rejected(self):
        real = Path(self.temp.name) / "real"
        real.mkdir()
        link = Path(self.temp.name) / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(Exception):
            ShadowEventStore(link)

    def test_root_permissions_are_private(self):
        self.assertEqual(os.stat(self.store.root).st_mode & 0o777, 0o700)

    def test_generated_health_permissions_are_private(self):
        self.maintenance.write_health_snapshot()
        self.assertEqual(os.stat(self.maintenance.health_path).st_mode & 0o777, 0o600)

    def test_canary_defaults_are_safe(self):
        config = ShadowCanaryConfig()
        self.assertEqual(config.validate(), [])
        self.assertFalse(config.enabled)
        self.assertFalse(config.central_claim_enabled)
        self.assertFalse(config.telegram_output_enabled)

    def test_non_antigravity_canary_is_rejected(self):
        errors = ShadowCanaryConfig(provider_role="codex").validate()
        self.assertTrue(errors)

    def test_canary_cannot_enable_central_claim(self):
        errors = ShadowCanaryConfig(central_claim_enabled=True).validate()
        self.assertIn("central claim must remain disabled", errors)

    def test_canary_requires_dedicated_key_when_enabled(self):
        errors = ShadowCanaryConfig(enabled=True, root=Path(self.temp.name) / "canary").validate()
        self.assertIn("enabled canary requires a dedicated HMAC key path", errors)


if __name__ == "__main__":
    unittest.main()
