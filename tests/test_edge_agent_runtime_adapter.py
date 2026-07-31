import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_efficiency_events import EfficiencyStore  # noqa: E402
from edge_agent_runtime_adapter import RuntimeEfficiencyAdapter, configured_mode, infer_kind  # noqa: E402


class RuntimeEfficiencyAdapterTests(unittest.TestCase):
    def test_mode_defaults_off_and_rejects_unknown_values(self):
        self.assertEqual(configured_mode("off").value, "off")
        with self.assertRaises(ValueError):
            configured_mode("always")

    def test_inference_is_deterministic(self):
        self.assertEqual(infer_kind("간단히 대화하자").value, "chat")
        self.assertEqual(infer_kind("이 기능을 조사하고 출처를 정리해줘").value, "research")
        self.assertEqual(infer_kind("파일을 수정하고 테스트해줘").value, "full_review")

    def test_off_mode_preserves_prompt_and_does_not_inject_context(self):
        adapter = RuntimeEfficiencyAdapter(mode="off")
        prepared = adapter.prepare("원본 요청", provider="claude", context="큰 참고자료")
        self.assertEqual(prepared.prompt, "원본 요청")
        self.assertEqual(prepared.context_chars, 0)

    def test_enforce_mode_bounds_and_selects_profile(self):
        adapter = RuntimeEfficiencyAdapter(mode="enforce")
        prepared = adapter.prepare(
            "파일 구현 " * 3000,
            provider="codex",
            context="참고자료 " * 3000,
            skill_documents={"verification": "테스트 규칙"},
        )
        self.assertLessEqual(len(prepared.prompt), prepared.budget.max_context_chars)
        self.assertEqual(prepared.profile.kind.value, "nano_mid")
        self.assertIn("reasoning_effort", prepared.cli_options())

    def test_record_writes_only_aggregate_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EfficiencyStore(directory)
            adapter = RuntimeEfficiencyAdapter(mode="enforce", event_store=store)
            prepared = adapter.prepare("테스트 요청", provider="gemma")
            result = adapter.record(
                prepared,
                task_id="task-runtime",
                step_id="step-1",
                status="passed",
                output="짧은 결과",
                verification_tier="light",
            )
            self.assertEqual(result, "recorded")
            line = store.path.read_text(encoding="utf-8")
            self.assertIn('"prompt_chars"', line)
            self.assertNotIn("테스트 요청", line)


if __name__ == "__main__":
    unittest.main()
