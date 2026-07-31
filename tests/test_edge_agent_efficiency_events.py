import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_efficiency_events import EfficiencyEvent, EfficiencyStore  # noqa: E402


class EfficiencyEventTests(unittest.TestCase):
    def event(self):
        return EfficiencyEvent(
            task_id="task-1",
            step_id="step-1",
            event_idempotency_key="task-1::step-1",
            provider="codex",
            profile="nano_light",
            status="passed",
            context_chars=100,
            prompt_chars=120,
            output_chars=80,
            tool_turns=2,
            changed_files=1,
            duration_ms=300,
            verification_tier="light",
        )

    def test_event_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EfficiencyStore(temp)
            event = self.event()
            self.assertEqual(store.append(event), "recorded")
            self.assertEqual(store.append(event), "duplicate")
            self.assertEqual(len(Path(temp, "events.jsonl").read_text().splitlines()), 1)

    def test_conflicting_event_and_sensitive_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EfficiencyStore(temp)
            store.append(self.event())
            changed = self.event().to_dict()
            changed.pop("schema", None)
            changed["output_chars"] = 81
            with self.assertRaises(ValueError):
                store.append(EfficiencyEvent(**changed))
            sensitive = self.event().to_dict()
            sensitive.pop("schema", None)
            sensitive["provider"] = "token=hidden"
            with self.assertRaises(ValueError):
                EfficiencyEvent(**sensitive)


if __name__ == "__main__":
    unittest.main()
