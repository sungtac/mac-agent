#!/usr/bin/env python3
import importlib.util
import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "audit-edge-agent-boundary.py"
SPEC = importlib.util.spec_from_file_location("edge_boundary_audit", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BoundaryAuditTests(unittest.TestCase):
    def _fixture(self):
        root = Path(tempfile.mkdtemp())
        manifest = root / "boundary.json"
        launch = root / "LaunchAgents"
        launch.mkdir()
        workspace = root / "workspace"
        team_os = workspace / "team_os"
        state = workspace / "state"
        telegram = workspace / "sukja_telegram"
        for path in (team_os, state, telegram):
            path.mkdir(parents=True)
        payload = {
            "schema": "edge_agent_workspace_boundary.v1",
            "mode": "audit_only",
            "edge_agent_source_root": str(root / "mac-agent"),
            "legacy_shared_workspace": str(workspace),
            "protected_roots": [str(team_os), str(state), str(telegram)],
            "runtime_services": {
                "telegram_claude": "com.macagent.telegram-claude",
                "telegram_roda_gemma": "com.macagent.telegram-roda-gemma",
            },
        }
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        claude_plist = launch / "com.macagent.telegram-claude.plist"
        with claude_plist.open("wb") as handle:
            plistlib.dump({
                "Label": "com.macagent.telegram-claude",
                "ProgramArguments": ["python3", "telegram-agent-bot.py"],
                "WorkingDirectory": str(workspace),
                "EnvironmentVariables": {"TELEGRAM_AGENT_ROLE": "claude", "TELEGRAM_AGENT_CHAT_ID": "redacted"},
            }, handle)
        roda_plist = launch / "com.macagent.telegram-roda-gemma.plist"
        with roda_plist.open("wb") as handle:
            plistlib.dump({
                "Label": "com.macagent.telegram-roda-gemma",
                "ProgramArguments": ["python3", "roda-gemma-bot.py"],
                "WorkingDirectory": str(root / "mac-agent"),
                "EnvironmentVariables": {"RODA_GEMMA_MODEL": "gemma4:latest"},
            }, handle)
        return root, manifest, launch, workspace

    def test_detects_shared_workspace_and_dirty_team_os_without_exposing_sensitive_env(self):
        _root, manifest, launch, workspace = self._fixture()
        report = MODULE.audit_boundary(
            manifest,
            launch_agents_dir=launch,
            process_lines=["12 1 python /repo/telegram-agent-bot.py"],
            worktree_text=f"worktree {workspace}\nbranch refs/heads/main\n",
            team_status_text=" M team_os/execution/approval.py\n?? state/new.json\n",
        )
        codes = {item.code for item in report.findings}
        self.assertIn("shared_workspace_overlap", codes)
        self.assertIn("team_workspace_dirty", codes)
        claude = next(item for item in report.services if item["label"] == "com.macagent.telegram-claude")
        self.assertEqual(claude["safe_environment"], {"TELEGRAM_AGENT_ROLE": "claude"})
        self.assertNotIn("CHAT_ID", json.dumps(report.to_dict()))

    def test_roda_workspace_is_not_marked_as_shared_when_it_is_mac_agent(self):
        _root, manifest, launch, _workspace = self._fixture()
        report = MODULE.audit_boundary(manifest, launch_agents_dir=launch, process_lines=[], team_status_text="")
        codes = {item.code for item in report.findings}
        self.assertNotIn("roda_workspace_overlap", codes)


if __name__ == "__main__":
    unittest.main()
