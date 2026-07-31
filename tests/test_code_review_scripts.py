import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / "skills" / "code-review" / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


NORMALIZER = load_script("normalize-review-request.py")
VALIDATOR = load_script("validate-review-report.py")


class CodeReviewScriptTests(unittest.TestCase):
    def test_all_korean_aliases_normalize_to_one_intent(self):
        for phrase in NORMALIZER.ALIASES:
            result = NORMALIZER.normalize_request(f"{phrase} 해줘", scope="files", paths=["src/app.py"])
            self.assertEqual(result["intent"], "code_review")
            self.assertEqual(result["scope"], "files")

    def test_scope_requires_paths_for_partial_review(self):
        with self.assertRaises(ValueError):
            NORMALIZER.normalize_request("코드 리뷰 해줘", scope="module")

    def test_approval_is_invalidated_by_different_sha(self):
        report = {
            "schema_version": "edge_agent.code_review_report.v1",
            "review_id": "r-1",
            "status": "AI_APPROVED",
            "target": {"scope": "diff", "head_sha": "new"},
            "findings": [],
            "checks": [{"name": "unit", "status": "passed"}],
            "approval": {
                "provider": "antigravity",
                "reviewed_head_sha": "old",
                "decision_reason": "verified",
            },
        }
        self.assertTrue(VALIDATOR.validate_report(report))

    def test_clean_report_is_valid(self):
        report = {
            "schema_version": "edge_agent.code_review_report.v1",
            "review_id": "r-2",
            "status": "AI_APPROVED",
            "target": {"scope": "diff", "head_sha": "same"},
            "findings": [],
            "checks": [{"name": "unit", "status": "passed"}],
            "approval": {
                "provider": "antigravity",
                "reviewed_head_sha": "same",
                "decision_reason": "No verified findings.",
            },
        }
        self.assertEqual(VALIDATOR.validate_report(report), [])

    def test_approval_requires_a_real_check(self):
        report = {
            "schema_version": "edge_agent.code_review_report.v1",
            "review_id": "r-3",
            "status": "AI_APPROVED",
            "target": {"scope": "diff", "head_sha": "same"},
            "findings": [],
            "checks": [],
            "approval": {
                "provider": "antigravity",
                "reviewed_head_sha": "same",
                "decision_reason": "verified",
            },
        }
        self.assertTrue(VALIDATOR.validate_report(report))

    def test_finding_category_must_match_schema(self):
        report = {
            "schema_version": "edge_agent.code_review_report.v1",
            "review_id": "r-4",
            "status": "REVIEWED",
            "target": {"scope": "diff", "head_sha": "same"},
            "findings": [
                {
                    "id": "f-1",
                    "severity": "medium",
                    "category": "not-a-category",
                    "location": "app.py:1",
                    "title": "Invalid category",
                    "evidence": "evidence",
                    "remediation": "fix it",
                }
            ],
            "checks": [],
        }
        self.assertTrue(VALIDATOR.validate_report(report))


if __name__ == "__main__":
    unittest.main()
