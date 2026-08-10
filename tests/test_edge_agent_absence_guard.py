import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "edge_agent_absence_guard.py"
SPEC = importlib.util.spec_from_file_location("edge_agent_absence_guard", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AbsenceGuardTests(unittest.TestCase):
    def test_unrelated_compound_word_near_negation_is_allowed(self):
        result = MODULE.validate_provider_payload({"summary": "패키지 의존성 변경은 없다"})
        self.assertTrue(result["validated"])

    def test_korean_key_absence_claim_with_particle_is_rejected(self):
        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):
            MODULE.validate_provider_payload({"summary": "API 키가 없습니다"})

    def test_korean_token_absence_claim_is_rejected(self):
        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):
            MODULE.validate_provider_payload({"summary": "토큰이 없음"})

    def test_bare_capability_absence_claim_is_rejected(self):
        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):
            MODULE.validate_provider_payload({"summary": "Telegram token is not configured"})

    def test_absence_claim_with_discovery_evidence_is_allowed(self):
        result = MODULE.validate_provider_payload({
            "summary": "not found in searched scope",
            "discovery_evidence": {"searched_scopes": ["environment", "/tmp/config"]},
        })
        self.assertTrue(result["validated"])

    def test_local_scan_finds_candidate_names_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / ".edge-agent" / "secrets"
            secrets.mkdir(parents=True)
            secret = secrets / "service.token"
            secret.write_text("must-not-be-read-or-rendered", encoding="utf-8")

            evidence = MODULE.discover_local_sources(home=root)
            encoded = json.dumps(evidence.as_dict(), ensure_ascii=False)

            self.assertTrue(evidence.complete)
            self.assertTrue(any(item.location == str(secret.resolve()) for item in evidence.candidate_sources))
            self.assertNotIn("must-not-be-read-or-rendered", encoded)

    def test_unrelated_words_across_sentence_boundary_are_not_rejected(self):
        result = MODULE.validate_provider_payload({
            "plan": "\ubaa8\ud638\uc131: \uc5c6\uc74c. \uc694\uad6c\uc0ac\ud56d\uacfc \uc218\uc815 \uacbd\uacc4\uac00 \uba85\ud655\ud558\ub2e4.\n\ud30c\uc77c \uc18c\uc720\uad8c\uc740 coach.py\ub85c \uc81c\ud55c\ud55c\ub2e4.",
        })
        self.assertTrue(result["validated"])

    def test_scoped_absence_requires_complete_search(self):
        incomplete = MODULE.DiscoveryEvidence("x", ("environment",), (), (), False, "now")
        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):
            MODULE.scoped_absence_claim("x", incomplete)

        complete = MODULE.DiscoveryEvidence("x", ("environment", "/tmp/config"), ("scan",), (), True, "now")
        claim = MODULE.scoped_absence_claim("x", complete)
        self.assertEqual(claim["status"], "not_found_in_searched_scope")

    def test_split_across_sentences_is_allowed(self):
        result = MODULE.validate_provider_payload({
            "summary": "The target file matches expectations. Later in a separate sentence we confirm nothing is missing here.",
        })
        self.assertTrue(result["validated"])

    def test_same_clause_absence_claim_still_rejected(self):
        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):
            MODULE.validate_provider_payload({"summary": "the config file is missing"})

    def test_diff_hunk_literal_is_not_scanned_as_claim(self):
        message = (
            "적용함.\n\n"
            "diff --git a/tests/example_test.py b/tests/example_test.py\n"
            "index 111..222 100644\n"
            "--- a/tests/example_test.py\n"
            "+++ b/tests/example_test.py\n"
            "@@\n"
            "+    def test_same_clause_absence_claim_still_rejected(self):\n"
            "+        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):\n"
            "+            MODULE.validate_provider_payload({\"summary\": \"the config file is missing\"})\n"
        )
        result = MODULE.validate_provider_payload({"ok": True, "message": message})
        self.assertTrue(result["validated"])

    def test_prose_claim_outside_diff_still_rejected(self):
        with self.assertRaises(MODULE.UnsupportedAbsenceClaim):
            MODULE.validate_provider_payload({
                "ok": True,
                "message": "적용함.\n\nthe api key is missing, so I skipped this step.\n",
            })


if __name__ == "__main__":
    unittest.main()
