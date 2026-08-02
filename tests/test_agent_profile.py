#!/usr/bin/env python3
"""Tests for the shared identity, persona, and response-style contract."""

import sys
import unittest
import json
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from agent_profile import load_profile_contract, render_agent_profile, resolve_persona  # noqa: E402


class AgentProfileTests(unittest.TestCase):
    def test_contract_contains_four_stable_roles(self):
        contract = load_profile_contract()
        self.assertEqual(set(contract["agents"]), {"claude", "codex", "antigravity", "roda"})
        self.assertEqual(contract["common_style"]["id"], "plain-high-school-v1")
        self.assertEqual(contract["common_style"]["forbidden_formatting"], ["###", "**"])

    def test_phase_personas_are_defined(self):
        self.assertEqual(resolve_persona("claude", phase="FullPromptify"), "planner")
        self.assertEqual(resolve_persona("codex", phase="FullExecute"), "implementer")
        self.assertEqual(resolve_persona("agy", phase="FullResearch"), "researcher")
        self.assertEqual(resolve_persona("gemini", phase="FullCodeReviewSkill"), "auditor")
        self.assertEqual(resolve_persona("gemma", phase="LocalProcessing"), "local-processor")

    def test_roda_profile_is_canonical(self):
        rendered = render_agent_profile("roda")
        self.assertIn("Roda (로다)", rendered)
        self.assertIn("로컬 Gemma4 처리·대화 에이전트", rendered)
        self.assertIn("로컬 처리 담당자", rendered)

    def test_rendered_profile_contains_identity_persona_and_shared_style(self):
        rendered = render_agent_profile("codex", "code-reviewer")
        self.assertIn("정밀 구현 및 검증 엔지니어", rendered)
        self.assertIn("현재 persona: code-reviewer", rendered)
        self.assertIn("고등학생 수준", rendered)
        self.assertIn("###", rendered)
        self.assertIn("**", rendered)

    def test_unknown_role_or_persona_fails_closed(self):
        with self.assertRaises(ValueError):
            resolve_persona("unknown")
        with self.assertRaises(ValueError):
            resolve_persona("codex", "researcher")

    def test_malformed_nested_contract_fails_with_value_error(self):
        malformed = {
            "schema": "edge_agent.agent_profile.v1",
            "version": "1.0.0",
            "common_style": {"id": "plain-high-school-v1", "audience": "일반 사용자", "rules": [], "forbidden_formatting": []},
            "agents": {"claude": {}, "codex": {}, "antigravity": {}},
        }
        with mock.patch("pathlib.Path.read_text", return_value=json.dumps(malformed)):
            with self.assertRaises(ValueError):
                load_profile_contract()


if __name__ == "__main__":
    unittest.main()
