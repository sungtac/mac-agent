import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"


class FakeSent:
    def __init__(self, message_id: int):
        self.message_id = message_id

    async def edit_text(self, text):
        return self


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def make_update(module, records):
    async def reply_text(text):
        records["replies"].append({"text": text})
        return FakeSent(len(records["replies"]) + 10)

    message = SimpleNamespace(
        text="상태 알려줘",
        caption=None,
        entities=None,
        caption_entities=None,
        from_user=SimpleNamespace(is_bot=False, id=1),
        chat_id=-1003952617795,
        message_id=10,
        reply_to_message=None,
        reply_text=reply_text,
    )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(type=module.ChatType.GROUP, id=message.chat_id),
    )


async def execute(module, records):
    def stable_value(value):
        if callable(value):
            return "<callable>"
        return str(value)

    async def provider(*args, **kwargs):
        records["provider_calls"].append({"args": [stable_value(value) for value in args], "kwargs": {key: stable_value(value) for key, value in kwargs.items()}})
        return "synthetic provider response"

    async def edit(text):
        records["edits"].append({"text": text})
        return FakeSent(11)

    class Progress:
        message_id = 11
        edit_text = edit

    def create_delivery(**kwargs):
        records["delivery"] = {key: stable_value(value) for key, value in kwargs.items() if key in {"task_id", "role", "chat_id", "source_message_id", "chunks"}}
        return {"delivery_id": "synthetic-delivery"}

    patches = [
        patch.object(module, "addressed_text", return_value="상태 알려줘"),
        patch.object(module, "_prepare_context", return_value=None),
        patch.object(module, "_is_stale", return_value=False),
        patch.object(module, "_needs_task_worktree", return_value=False),
        patch.object(module, "_is_delivery_retry_request", return_value=False),
        patch.object(module, "run_provider", new=provider),
        patch.object(module, "write_task_state", return_value="task-1"),
        patch.object(module, "start_session", return_value="session-1"),
        patch.object(module, "update_session"),
        patch.object(module, "write_reflection"),
        patch.object(module, "_record_telegram_efficiency"),
        patch.object(module, "create_delivery", side_effect=create_delivery),
        patch.object(module, "mark_chunk_sent"),
        patch.object(module, "mark_delivery_succeeded"),
        patch.object(module, "_update_task_worktree_status"),
        patch.object(module, "log"),
        patch.object(module, "ROLE", "claude"),
    ]
    for item in patches:
        item.start()
    try:
        await module.handle_message(make_update(module, records), SimpleNamespace())
    finally:
        for item in reversed(patches):
            item.stop()


class ShadowOffPayloadEqualityTests(unittest.TestCase):
    def test_clean_head_and_integration_flag_off_payloads_match(self):
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp) / "fake.token"
            token.write_text("123456:unit-test-token", encoding="utf-8")
            token.chmod(0o600)
            baseline_bin = Path(temp) / "baseline-bin"
            baseline_bin.mkdir()
            baseline_source = subprocess.check_output(
                ["git", "show", "HEAD:bin/telegram-agent-bot.py"], cwd=ROOT, text=True
            )
            baseline_path = baseline_bin / "telegram-agent-bot.py"
            baseline_path.write_text(baseline_source, encoding="utf-8")
            old_env = dict(os.environ)
            os.environ.update({
                "TELEGRAM_AGENT_ROLE": "claude",
                "TELEGRAM_AGENT_CHAT_ID": "-1003952617795",
                "TELEGRAM_AGENT_TOKEN_FILE": str(token),
                "EDGE_AGENT_SHADOW_OBSERVER_ENABLED": "0",
                "EDGE_AGENT_SHADOW_ROOT": str(Path(temp) / "shadow"),
            })
            try:
                baseline = load_module(baseline_path, "telegram_agent_bot_baseline_payload")
                candidate = load_module(BIN / "telegram-agent-bot.py", "telegram_agent_bot_candidate_payload")
                baseline_records = {"replies": [], "edits": [], "provider_calls": [], "delivery": {}}
                candidate_records = {"replies": [], "edits": [], "provider_calls": [], "delivery": {}}
                asyncio.run(execute(baseline, baseline_records))
                asyncio.run(execute(candidate, candidate_records))
                self.assertEqual(baseline_records, candidate_records)
            finally:
                os.environ.clear()
                os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
