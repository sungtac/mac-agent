import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from skills.hermes_runtime.hermes_backlog import build_backlog  # noqa: E402
from skills.hermes_runtime.hermes_lifecycle_evidence import apply_evidence  # noqa: E402
from skills.hermes_runtime.hermes_lifecycle_gate import live_evidence, recurrence_free_window, retirement_evidence  # noqa: E402
from skills.quota_resume import quota_resume  # noqa: E402


class SkillSafetyRegressionTests(unittest.TestCase):
    def test_short_hermes_evidence_is_not_live_proof(self):
        self.assertFalse(live_evidence({"liveEvidence": "x"}))
        self.assertTrue(live_evidence({"liveEvidence": "runtime probe completed with messageId 12345"}))

    def test_hermes_backlog_survives_non_object_json_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.jsonl"
            path.write_text("[]\n", encoding="utf-8")
            report = build_backlog(path)
            self.assertEqual(report["total"], 1)
            self.assertEqual(report["items"][0]["status"], "blocked")

    def test_hermes_outputs_redact_legacy_secrets_and_evidence_rejects_them(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.jsonl"
            path.write_text(json.dumps({"title": "legacy token=sk-test", "status": "blocked", "validation": "api_key=secret-value", "risk": "password=another-secret", "files": "skills/secret.py"}) + "\n", encoding="utf-8")
            report = build_backlog(path)
            output = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("sk-test", output)
            self.assertNotIn("secret-value", output)
            self.assertEqual(report["items"][0]["files_changed"], ("skills/secret.py",))

            denied = apply_evidence(
                path,
                title="legacy token=sk-test",
                target_stage="live_verified",
                live_evidence_text="runtime probe token=sk-test completed",
                approved=True,
            )
            self.assertFalse(denied.ok)
            self.assertIn("sensitive", " ".join(denied.errors))

    def test_hermes_apply_requires_approval_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.jsonl"
            path.write_text(json.dumps({"title": "Fix runtime", "status": "mitigated", "priority": 100, "validation": "unit tests passed"}) + "\n", encoding="utf-8")
            denied = apply_evidence(path, title="Fix runtime", target_stage="live_verified", live_evidence_text="runtime probe completed with messageId 12345")
            self.assertFalse(denied.ok)
            self.assertIn("explicit approval", " ".join(denied.errors))
            approved = apply_evidence(path, title="Fix runtime", target_stage="live_verified", live_evidence_text="runtime probe completed with messageId 12345", approved=True)
            self.assertTrue(approved.ok)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["lifecycleStage"], "live_verified")

    def test_hermes_retirement_requires_recurrence_free_window(self):
        record = {
            "status": "retired",
            "validation": "unit tests passed",
            "liveEvidence": "runtime probe completed with messageId 12345",
            "retirementEvidence": "no recurrence observed during review",
            "retiredAt": "2026-08-02T00:00:00Z",
        }
        self.assertTrue(retirement_evidence(record))
        self.assertFalse(recurrence_free_window(record))
        record["recurrenceFreeWindow"] = "review window"
        self.assertFalse(recurrence_free_window(record))
        record["recurrenceFreeWindow"] = "14 days without recurrence"
        self.assertTrue(recurrence_free_window(record))

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.jsonl"
            path.write_text(json.dumps({"title": "Fix runtime", "status": "mitigated", "priority": 100, "validation": "unit tests passed", "liveEvidence": "runtime probe completed with messageId 12345"}) + "\n", encoding="utf-8")
            denied = apply_evidence(
                path,
                title="Fix runtime",
                target_stage="retired",
                live_evidence_text="runtime probe completed with messageId 12345",
                retirement_evidence_text="no recurrence observed during review",
                approved=True,
            )
            self.assertFalse(denied.ok)
            self.assertIn("recurrence-free window", " ".join(denied.errors))
            approved = apply_evidence(
                path,
                title="Fix runtime",
                target_stage="retired",
                live_evidence_text="runtime probe completed with messageId 12345",
                retirement_evidence_text="no recurrence observed during review",
                recurrence_free_window_text="14 days without recurrence",
                approved=True,
            )
            self.assertTrue(approved.ok)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["recurrenceFreeWindow"], "14 days without recurrence")

    def test_quota_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            quota_resume.ensure_state_files(base)
            state = base / "state" / "quota_events.json"
            state.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                quota_resume.record_quota_event(base, {"message": "429"})

    def test_quota_redacts_event_id_and_uses_private_state_directories(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            quota_resume.ensure_state_files(base)
            event = quota_resume.record_quota_event(base, {"event_id": "token=sk-sensitive-value", "message": "429"})
            self.assertNotIn("sk-sensitive-value", event["event_id"])
            self.assertEqual((base / "state").stat().st_mode & 0o777, 0o700)
            saved = quota_resume.record_quota_event(base, {"event_id": "structured", "api_key": "short-value"})
            self.assertNotIn("short-value", json.dumps(saved, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
