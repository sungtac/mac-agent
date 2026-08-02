import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("edge_agent_engine_retirement", ROOT / "bin" / "edge_agent_engine_retirement.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class EngineRetirementGateTests(unittest.TestCase):
    def test_gate_requires_approval_but_proves_rollback_and_shared_owner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "mac-agent"
            agents = root / "LaunchAgents"
            engine = root / "engine-repo"
            (source / "bin").mkdir(parents=True)
            (source / "docs").mkdir()
            agents.mkdir()
            engine.mkdir()
            (source / "bin" / "telegram-agent-bot.py").write_text('ROLES = {"claude": {}, "antigravity": {}}\n', encoding="utf-8")
            (source / "docs" / "engine-canonical-migration-2026-08-02.md").write_text("## 오프라인 canary 결과\n", encoding="utf-8")
            (agents / "com.multiagent.engine.plist").write_bytes(plistlib.dumps({
                "Label": "com.multiagent.engine",
                "ProgramArguments": ["python3", str(engine / "telegram" / "adapter.py")],
            }))
            (agents / "com.macagent.telegram-codex.plist").write_bytes(plistlib.dumps({
                "Label": "com.macagent.telegram-codex",
                "Disabled": True,
                "ProgramArguments": ["python3", str(source / "bin" / "telegram-agent-bot.py")],
            }))
            report = MODULE.audit(source_root=source, launch_agents=agents, engine_repo=engine)
            self.assertTrue(report["ready_for_approval_review"])
            self.assertFalse(report["rollback"]["service_mutation_performed"])
            self.assertTrue(report["rollback"]["temporary_copy_rehearsal"]["passed"])
            self.assertEqual({item["id"] for item in report["approval_required"]}, {
                "telegram_canary_send", "direct_codex_plist_quarantine", "shared_adapter_codex_split",
            })


if __name__ == "__main__":
    unittest.main()
