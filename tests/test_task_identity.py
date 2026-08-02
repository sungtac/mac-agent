import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_task_identity import (  # noqa: E402
    canonical_json,
    child_task_id,
    root_task_id,
    run_id,
)


class TaskIdentityTests(unittest.TestCase):
    def group_fields(self, **extra):
        fields = {
            "platform": "telegram",
            "chat_scope": "group",
            "shared_chat_id": "-1001",
            "message_id": 42,
        }
        fields.update(extra)
        return fields

    def test_group_message_is_same_across_provider_bots(self):
        first = root_task_id(**self.group_fields(bot_id="claude"))
        second = root_task_id(**self.group_fields(bot_id="codex"))
        self.assertEqual(first, second)

    def test_private_bot_identity_does_not_collide(self):
        first = root_task_id(platform="telegram", chat_scope="private", bot_id="claude", chat_id="7", message_id=42)
        second = root_task_id(platform="telegram", chat_scope="private", bot_id="codex", chat_id="7", message_id=42)
        self.assertNotEqual(first, second)

    def test_route_and_task_fields_do_not_change_root(self):
        base = root_task_id(**self.group_fields())
        changed = root_task_id(**self.group_fields(
            task_type="coding", risk_level="HIGH", classification_version="v2",
            route_policy_version="v9", assigned_provider="claude", assigned_agent="reviewer",
            retry_count=99, message_text="changed text", attachment_hash="changed",
        ))
        self.assertEqual(base, changed)

    def test_retry_and_child_keep_parent_identity(self):
        root = root_task_id(**self.group_fields())
        child = child_task_id(root, 0, "independent_reviewer")
        retry = child_task_id(root, 0, "independent_reviewer")
        self.assertEqual(child, retry)
        self.assertEqual(root, root_task_id(**self.group_fields(retry_count=1)))

    def test_run_ids_are_distinct_for_attempts(self):
        root = root_task_id(**self.group_fields())
        first = run_id(root, 1, nonce="a")
        second = run_id(root, 2, nonce="b")
        self.assertNotEqual(first, second)

    def test_canonical_field_order_is_stable(self):
        left = {"z": 1, "a": {"y": 2, "x": 3}}
        right = {"a": {"x": 3, "y": 2}, "z": 1}
        self.assertEqual(canonical_json(left), canonical_json(right))

    def test_python_restart_produces_same_hash(self):
        fields = self.group_fields()
        code = "import json,sys; sys.path.insert(0, sys.argv[1]); from edge_agent_task_identity import root_task_id; print(root_task_id(**json.loads(sys.argv[2])))"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        first = subprocess.check_output([sys.executable, "-c", code, str(ROOT / "bin"), json.dumps(fields)], env=env, text=True).strip()
        second = subprocess.check_output([sys.executable, "-c", code, str(ROOT / "bin"), json.dumps(fields)], env=env, text=True).strip()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
