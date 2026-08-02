import plistlib
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from edge_agent_auth_boundary import audit  # noqa: E402


class EdgeAgentAuthBoundaryTests(unittest.TestCase):
    @staticmethod
    def loaded_runner(stdout: str, returncode: int = 0):
        def runner(argv, **kwargs):
            return type("Result", (), {"stdout": stdout, "stderr": "", "returncode": returncode})()
        return runner

    def test_legacy_calendar_is_reported_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            legacy = home / ".openclaw" / "secrets"
            legacy.mkdir(parents=True)
            (legacy / "google_calendar_token.json").write_text("secret", encoding="utf-8")
            report = audit(home=home, launch_agents_dir=home / "LaunchAgents")
            self.assertEqual(report["credentials"]["calendar_token"]["status"], "needs_migration")
            self.assertFalse(report["ready"])

    def test_canonical_file_and_launchagent_are_ready(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            root = home / ".edge-agent" / "secrets"
            (root / "calendar").mkdir(parents=True)
            token = root / "calendar" / "google_calendar_token.json"
            token.write_text("not inspected", encoding="utf-8")
            token.chmod(0o600)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            plist = {
                "EnvironmentVariables": {
                    "TELEGRAM_AGENT_TOKEN_FILE": str(root / "telegram" / "claude.token"),
                }
            }
            (agents / "com.macagent.telegram-claude.plist").write_bytes(plistlib.dumps(plist))
            report = audit(home=home, launch_agents_dir=agents)
            self.assertEqual(report["credentials"]["calendar_token"]["status"], "ready")
            self.assertEqual(report["credentials"]["calendar_token"]["canonical"]["mode"], "0600")

    def test_loaded_launchd_path_drift_is_blocking_without_reading_token(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            root = home / ".edge-agent" / "secrets"
            (root / "telegram").mkdir(parents=True)
            token = root / "telegram" / "claude.token"
            token.write_text("not inspected", encoding="utf-8")
            token.chmod(0o600)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            plist = {
                "EnvironmentVariables": {
                    "TELEGRAM_AGENT_TOKEN_FILE": str(token),
                }
            }
            (agents / "com.macagent.telegram-claude.plist").write_bytes(plistlib.dumps(plist))
            old = "/tmp/legacy-token"
            loaded = "environment = {\n\tTELEGRAM_AGENT_TOKEN_FILE => " + old + "\n}\n"
            report = audit(
                home=home,
                launch_agents_dir=agents,
                launchctl_uid=501,
                launchctl_runner=self.loaded_runner(loaded),
            )
            self.assertIn("launchd_loaded_drift:com.macagent.telegram-claude.plist", report["blocking_items"])
            self.assertEqual(report["launch_agents"]["com.macagent.telegram-claude.plist"]["loaded"]["configured_path"], old)

    def test_launchagent_without_expected_path_is_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            (agents / "com.macagent.telegram-claude.plist").write_bytes(plistlib.dumps({"EnvironmentVariables": {}}))
            report = audit(home=home, launch_agents_dir=agents)
            self.assertIn("launchd:com.macagent.telegram-claude.plist", report["blocking_items"])

    def test_unregistered_launchagent_reusing_token_is_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            root = home / ".edge-agent" / "secrets"
            telegram = root / "telegram"
            telegram.mkdir(parents=True)
            token = telegram / "codex.token"
            token.write_text("not inspected", encoding="utf-8")
            token.chmod(0o600)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            canonical = {
                "EnvironmentVariables": {"TELEGRAM_AGENT_TOKEN_FILE": str(token)},
            }
            (agents / "com.macagent.telegram-codex.plist").write_bytes(plistlib.dumps(canonical))
            legacy = {
                "EnvironmentVariables": {"TELEGRAM_BOT_TOKEN_FILE": str(token)},
            }
            (agents / "com.unregistered.telegram-consumer.plist").write_bytes(plistlib.dumps(legacy))

            report = audit(home=home, launch_agents_dir=agents)

            self.assertEqual(len(report["duplicate_token_consumers"]), 1)
            self.assertIn(
                "launchd_duplicate_token_consumer:com.unregistered.telegram-consumer.plist",
                report["blocking_items"],
            )

    def test_disabled_legacy_launchagent_is_not_counted_as_active_consumer(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            root = home / ".edge-agent" / "secrets"
            telegram = root / "telegram"
            telegram.mkdir(parents=True)
            token = telegram / "codex.token"
            token.write_text("not inspected", encoding="utf-8")
            token.chmod(0o600)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            (agents / "com.unregistered.telegram-consumer.plist").write_bytes(plistlib.dumps({
                "Disabled": True,
                "EnvironmentVariables": {"TELEGRAM_BOT_TOKEN_FILE": str(token)},
            }))

            report = audit(home=home, launch_agents_dir=agents)

            self.assertEqual(report["duplicate_token_consumers"], [])


if __name__ == "__main__":
    unittest.main()
