import tempfile
import unittest
from pathlib import Path

import sys

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from migrate_edge_agent_auth import plan  # noqa: E402


class EdgeAgentAuthMigrationTests(unittest.TestCase):
    def test_default_plan_never_copies(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            source = home / ".openclaw/secrets/google_calendar_token.json"
            source.parent.mkdir(parents=True)
            source.write_text("opaque", encoding="utf-8")
            report = plan(home=home, selected={"calendar_token"})
            self.assertEqual(report["records"][0]["status"], "planned_copy")
            self.assertFalse((home / ".edge-agent/secrets/calendar/google_calendar_token.json").exists())

    def test_copy_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            source = home / ".openclaw/secrets/google_calendar_token.json"
            source.parent.mkdir(parents=True)
            source.write_text("opaque", encoding="utf-8")
            report = plan(home=home, selected={"calendar_token"}, apply=True, confirm_copy=False)
            self.assertEqual(report["records"][0]["status"], "confirmation_required")
            self.assertFalse((home / ".edge-agent/secrets/calendar/google_calendar_token.json").exists())

    def test_copy_does_not_overwrite_existing_canonical_file(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            source = home / ".openclaw/secrets/google_calendar_token.json"
            destination = home / ".edge-agent/secrets/calendar/google_calendar_token.json"
            source.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            destination.write_text("existing", encoding="utf-8")
            report = plan(home=home, selected={"calendar_token"}, apply=True, confirm_copy=True)
            self.assertEqual(report["records"][0]["status"], "destination_exists")
            self.assertEqual(destination.read_text(encoding="utf-8"), "existing")

    def test_confirmed_copy_preserves_source_and_restricts_destination(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            source = home / ".openclaw/secrets/google_calendar_token.json"
            source.parent.mkdir(parents=True)
            source.write_text("opaque", encoding="utf-8")
            source.chmod(0o600)
            report = plan(home=home, selected={"calendar_token"}, apply=True, confirm_copy=True)
            destination = home / ".edge-agent/secrets/calendar/google_calendar_token.json"
            self.assertEqual(report["records"][0]["status"], "copied")
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_text(encoding="utf-8"), "opaque")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertTrue(source.is_file())

    def test_symlink_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            source = home / ".openclaw/secrets/google_calendar_token.json"
            target = home / "real-secret"
            source.parent.mkdir(parents=True)
            target.write_text("opaque", encoding="utf-8")
            source.symlink_to(target)
            report = plan(home=home, selected={"calendar_token"}, apply=True, confirm_copy=True)
            self.assertEqual(report["records"][0]["status"], "source_symlink_rejected")
            self.assertFalse((home / ".edge-agent/secrets/calendar/google_calendar_token.json").exists())

    def test_symlink_destination_is_rejected_even_with_replace(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            source = home / ".openclaw/secrets/google_calendar_token.json"
            destination = home / ".edge-agent/secrets/calendar/google_calendar_token.json"
            target = home / "destination-target"
            source.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            source.chmod(0o600)
            target.write_text("must survive", encoding="utf-8")
            destination.symlink_to(target)
            report = plan(home=home, selected={"calendar_token"}, apply=True, confirm_copy=True, allow_replace=True)
            self.assertEqual(report["records"][0]["status"], "destination_symlink_rejected")
            self.assertEqual(target.read_text(encoding="utf-8"), "must survive")


if __name__ == "__main__":
    unittest.main()
