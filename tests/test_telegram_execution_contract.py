#!/usr/bin/env python3
"""Focused tests for Telegram execution handoff and worktree ownership."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import TelegramError


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
from edge_agent_telegram_delivery import create_delivery, load_delivery  # noqa: E402
_ENV_KEYS = ("TELEGRAM_AGENT_ROLE", "TELEGRAM_AGENT_CHAT_ID", "TELEGRAM_AGENT_TOKEN_FILE")
_ENV_SNAPSHOT = {key: os.environ.get(key) for key in _ENV_KEYS}
os.environ["TELEGRAM_AGENT_ROLE"] = "claude"
os.environ["TELEGRAM_AGENT_CHAT_ID"] = "-1003952617795"
os.environ["TELEGRAM_AGENT_TOKEN_FILE"] = str(Path.home() / ".config" / "agent-telegram" / "claude.token")
SPEC = importlib.util.spec_from_file_location("telegram_agent_bot_execution_contract", BIN / "telegram-agent-bot.py")
BOT = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(BOT)
for _key, _value in _ENV_SNAPSHOT.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


class FakeSent:
    def __init__(self, message_id: int, delivery_error: Exception | None = None):
        self.message_id = message_id
        self.delivery_error = delivery_error

    async def edit_text(self, text):
        if self.delivery_error:
            raise self.delivery_error
        return self


class FakeBot:
    def __init__(self):
        self.edited = []

    async def edit_message_text(self, *, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))
        return FakeSent(message_id)


def make_update(sent: FakeSent):
    async def reply_text(text):
        return sent

    message = SimpleNamespace(
        text="클로드야 테스트해줘",
        caption=None,
        entities=None,
        caption_entities=None,
        from_user=SimpleNamespace(is_bot=False, id=1),
        chat_id=-1003952617795,
        message_id=10,
        reply_to_message=None,
        reply_text=reply_text,
    )
    chat = SimpleNamespace(type=BOT.ChatType.GROUP, id=-1003952617795)
    return SimpleNamespace(effective_message=message, effective_chat=chat)


class TelegramExecutionContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.delivery_root = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.delivery_root.cleanup()

    async def test_existing_worktree_requires_matching_owner_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "task-1"
            target.mkdir()
            (target / ".edge-agent-task.json").write_text(json.dumps({
                "schema": "edge_agent_worktree.v1",
                "task_id": "task-1",
                "role": "claude",
            }), encoding="utf-8")
            with patch.object(BOT, "CODEX_TASK_WORKTREE_ROOT", root), patch.object(BOT, "ROLE", "claude"):
                self.assertEqual(await BOT._create_task_worktree("task-1"), target)
                (target / ".edge-agent-task.json").write_text(json.dumps({
                    "schema": "edge_agent_worktree.v1",
                    "task_id": "other-task",
                    "role": "claude",
                }), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "다른 작업"):
                    await BOT._create_task_worktree("task-1")

    async def test_claude_coding_delegation_uses_independent_verification_loop(self):
        with patch.object(BOT, "codex_verify_and_revise", new=AsyncMock(return_value="verified")) as verify:
            result = await BOT.claude_delegates_to_codex("수정해줘", object(), chat_id="chat-1")
        verify.assert_awaited_once()
        self.assertIn("독립 검증한 결과", result)

    async def test_delivery_failure_remains_handoff_ready(self):
        updates = []
        tasks = []
        reflections = []
        sent = FakeSent(11, TelegramError("delivery failed"))
        with patch.object(BOT, "_prepare_context", return_value=None), \
                patch.object(BOT, "_is_stale", return_value=False), \
                patch.object(BOT, "_needs_task_worktree", return_value=False), \
                patch.object(BOT, "run_provider", new=AsyncMock(return_value="provider result")), \
                patch.object(BOT, "write_task_state", side_effect=lambda **kwargs: tasks.append(kwargs) or "task-1"), \
                patch.object(BOT, "start_session", return_value="session-1"), \
                patch.object(BOT, "update_session", side_effect=lambda *args, **kwargs: updates.append(kwargs)), \
                patch.object(BOT, "write_reflection", side_effect=lambda **kwargs: reflections.append(kwargs)), \
                patch.object(BOT, "_record_telegram_efficiency"), \
                patch.dict(os.environ, {"EDGE_AGENT_TELEGRAM_DELIVERY_ROOT": self.delivery_root.name}), \
                patch.object(BOT, "ROLE", "claude"):
            BOT.ACTIVE_TASK_WORKSPACE = None
            BOT.ACTIVE_LOGICAL_SESSION_ID = None
            await BOT.handle_message(make_update(sent), SimpleNamespace())

        self.assertEqual(updates[-1]["status"], "handoff_ready")
        self.assertEqual(updates[-1]["event_type"], "telegram_delivery_failed")
        self.assertEqual(tasks[-1]["status"], "delivery_pending")
        self.assertEqual(reflections[-1]["status"], "delivery_pending")

    async def test_successful_delivery_closes_handoff(self):
        updates = []
        tasks = []
        sent = FakeSent(11)
        with patch.object(BOT, "_prepare_context", return_value=None), \
                patch.object(BOT, "_is_stale", return_value=False), \
                patch.object(BOT, "_needs_task_worktree", return_value=False), \
                patch.object(BOT, "run_provider", new=AsyncMock(return_value="provider result")), \
                patch.object(BOT, "write_task_state", side_effect=lambda **kwargs: tasks.append(kwargs) or "task-1"), \
                patch.object(BOT, "start_session", return_value="session-1"), \
                patch.object(BOT, "update_session", side_effect=lambda *args, **kwargs: updates.append(kwargs)), \
                patch.object(BOT, "write_reflection"), \
                patch.object(BOT, "_record_telegram_efficiency"), \
                patch.dict(os.environ, {"EDGE_AGENT_TELEGRAM_DELIVERY_ROOT": self.delivery_root.name}), \
                patch.object(BOT, "ROLE", "claude"):
            BOT.ACTIVE_TASK_WORKSPACE = None
            BOT.ACTIVE_LOGICAL_SESSION_ID = None
            await BOT.handle_message(make_update(sent), SimpleNamespace())

        self.assertEqual(updates[-1]["status"], "succeeded")
        self.assertEqual(updates[-1]["event_type"], "telegram_delivery_succeeded")
        self.assertFalse(updates[-1]["verification"]["telegram_delivery_pending"])
        self.assertEqual(tasks[-1]["status"], "completed")

    async def test_delivery_retry_sends_pending_chunks_without_provider(self):
        fake_bot = FakeBot()
        sent = FakeSent(12)
        with patch.dict(os.environ, {"EDGE_AGENT_TELEGRAM_DELIVERY_ROOT": self.delivery_root.name}):
            delivery = create_delivery(
                task_id="task-retry",
                role="claude",
                chat_id="-1003952617795",
                owner_user_id="1",
                source_message_id="20",
                session_id="",
                request_preview="원 요청",
                chunks=["첫 chunk", "둘째 chunk"],
                progress_message_id="11",
            )
            with patch.object(BOT, "write_task_state"), \
                    patch.object(BOT, "write_reflection"), \
                    patch.object(BOT, "update_session"), \
                    patch.object(BOT, "run_provider", new=AsyncMock(side_effect=AssertionError("provider must not run"))):
                await BOT._handle_delivery_retry(
                    make_update(sent).effective_message,
                    SimpleNamespace(bot=fake_bot),
                )
            self.assertEqual(fake_bot.edited, [(-1003952617795, 11, "첫 chunk")])
            self.assertEqual(load_delivery(delivery["delivery_id"], root=self.delivery_root.name)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
