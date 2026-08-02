import sys
from pathlib import Path

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from edge_agent_router_contract import (  # noqa: E402
    ExecutionMode,
    Provider,
    RiskLevel,
    RoleAssignment,
    RouterDecision,
    RouterInput,
    RouterRole,
    TaskType,
)


class RouterContractTests(unittest.TestCase):
    def test_router_input_normalizes_and_validates_bounds(self):
        request = RouterInput("  설명해줘  ", attachment_count=2)
        self.assertEqual(request.text, "설명해줘")
        self.assertEqual(request.attachment_count, 2)


    def test_router_decision_serializes_stable_provider_neutral_shape(self):
        decision = RouterDecision(
            task_type=TaskType.WRITING,
            risk_level=RiskLevel.MEDIUM,
            execution_mode=ExecutionMode.SINGLE,
            roles=(RoleAssignment(RouterRole.WRITER, Provider.CODEX),),
            requires_approval=True,
        )
        payload = decision.to_dict()
        self.assertEqual(payload["roles"], [{"role": "writer", "provider": "codex", "dependencies": []}])
        self.assertEqual(decision.providers, (Provider.CODEX,))


    def test_router_rejects_more_than_four_providers(self):
        roles = tuple(RoleAssignment(RouterRole.WRITER, provider) for provider in (Provider.CODEX, Provider.GEMMA, Provider.ANTIGRAVITY))
        roles = roles + (RoleAssignment(RouterRole.REVIEWER, Provider.CLAUDE),)
        decision = RouterDecision(TaskType.DOCUMENT, RiskLevel.MEDIUM, ExecutionMode.TEAM, roles)
        self.assertEqual(len(decision.providers), 4)


if __name__ == "__main__":
    unittest.main()
