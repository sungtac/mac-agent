import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SkillMigrationContractTests(unittest.TestCase):
    def test_calendar_documents_only_edge_secret_root(self):
        skill = (ROOT / "skills/calendar/SKILL.md").read_text(encoding="utf-8")
        helper = (ROOT / "skills/calendar/google_calendar.py").read_text(encoding="utf-8")
        self.assertIn("~/.edge-agent/secrets/calendar", skill)
        self.assertNotIn("~/.openclaw/secrets", skill)
        self.assertIn("~/.edge-agent/secrets/calendar", helper)
        self.assertNotIn("~/.openclaw/secrets", helper)

    def test_calendar_rejects_legacy_override(self):
        source = (ROOT / "skills/calendar/google_calendar.py").read_text(encoding="utf-8")
        self.assertIn("Legacy OpenClaw credential paths are not supported", source)

    def test_runtime_defaults_use_edge_secret_root(self):
        telegram = (ROOT / "bin/telegram-agent-bot.py").read_text(encoding="utf-8")
        roda = (ROOT / "bin/roda-gemma-bot.py").read_text(encoding="utf-8")
        health = (ROOT / "bin/roda-telegram-health-monitor.py").read_text(encoding="utf-8")
        self.assertIn('.edge-agent" / "secrets" / "telegram"', telegram)
        self.assertIn(".edge-agent/secrets/roda-gemma/telegram.token", roda)
        self.assertIn(".edge-agent/secrets/roda-gemma/telegram.token", health)

    def test_roda_loads_common_skill_context(self):
        source = (ROOT / "bin/roda-gemma-bot.py").read_text(encoding="utf-8")
        runtime = (ROOT / "bin/edge_agent_channel_runtime.py").read_text(encoding="utf-8")
        self.assertIn("from edge_agent_channel_runtime import build_shared_context", source)
        self.assertIn("build_shared_context(", source)
        self.assertIn("from edge_agent_skill_connector import build_skill_context", runtime)
        self.assertIn("build_skill_context(request", runtime)

    def test_quota_resume_declared_modules_exist_and_are_preview_only(self):
        quota_root = ROOT / "skills/quota_resume"
        for name in (
            "quota_resume.py",
            "quota_resume_wrapper.py",
            "fallback_switch_preview.py",
            "hermes_quota_event_hook.py",
            "runtime_flags.py",
            "supervisor_checkpoint_hook.py",
            "telegram_notification_candidate.py",
        ):
            self.assertTrue((quota_root / name).is_file(), name)
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("EDGE_AGENT_STATE_ROOT")
            os.environ["EDGE_AGENT_STATE_ROOT"] = td
            try:
                from skills.quota_resume.runtime_flags import load_flags

                self.assertEqual(load_flags()["quota_resume_auto_execute"], False)
                self.assertEqual(load_flags()["requires_user_review"], True)
            finally:
                if old is None:
                    os.environ.pop("EDGE_AGENT_STATE_ROOT", None)
                else:
                    os.environ["EDGE_AGENT_STATE_ROOT"] = old


if __name__ == "__main__":
    unittest.main()
