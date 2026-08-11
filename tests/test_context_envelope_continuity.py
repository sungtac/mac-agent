#!/usr/bin/env python3
"""Continuity, isolation, redaction, and adapter guard tests."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
import importlib.util
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
_TOKEN_ROOT = tempfile.TemporaryDirectory()
_TOKEN_FILE = Path(_TOKEN_ROOT.name) / "claude.token"
_TOKEN_FILE.write_text("123456:unit-test-token", encoding="utf-8")
_TOKEN_FILE.chmod(0o600)

from edge_agent_context_envelope import (  # noqa: E402
    ContextEnvelopeStore,
    EntityAnchor,
    SCHEMA_VERSION,
    extract_anchor,
    looks_like_anaphoric_reference,
    native_session_path,
)


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    with patch.dict(os.environ, {
        "TELEGRAM_AGENT_ROLE": "claude",
        "TELEGRAM_AGENT_CHAT_ID": "-1003952617795",
        "TELEGRAM_AGENT_TOKEN_FILE": str(_TOKEN_FILE),
    }):
        spec.loader.exec_module(module)
    return module


class FakeSent:
    def __init__(self, message_id: int):
        self.message_id = message_id

    async def edit_text(self, text):
        return self


class ContextEnvelopeContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ContextEnvelopeStore(self.tempdir.name)
        self._previous_state_dir = os.environ.get("EDGE_AGENT_STATE_DIR")
        os.environ["EDGE_AGENT_STATE_DIR"] = str(Path(self.tempdir.name) / "task-state")
        self.bot = load_module("telegram_agent_bot_native_session_test", "telegram-agent-bot.py")

    def tearDown(self):
        if self._previous_state_dir is None:
            os.environ.pop("EDGE_AGENT_STATE_DIR", None)
        else:
            os.environ["EDGE_AGENT_STATE_DIR"] = self._previous_state_dir
        self.tempdir.cleanup()

    def test_video_anchor_resolves_short_fact_check_follow_up(self):
        first = self.store.prepare(
            channel="telegram", provider="claude", chat_id="chat-1", message_id=10,
            reply_to_message_id=None, text="08:44 영상 https://example.com/watch?v=private", now="2026-08-01T08:44:00+00:00",
        )
        second = self.store.prepare(
            channel="telegram", provider="claude", chat_id="chat-1", message_id=11,
            reply_to_message_id=None, text="팩트체크 해줘", now="2026-08-01T08:47:00+00:00",
        )
        self.assertEqual(first.resolution.status, "resolved")
        self.assertEqual(second.resolution.status, "resolved")
        self.assertIn("https://example.com/watch", second.prompt_block)
        self.assertNotIn("?v=private", second.prompt_block)
        self.assertTrue(looks_like_anaphoric_reference("팩트체크 해줘", self.store.anchors(channel="telegram", chat_id="chat-1", now="2026-08-01T08:47:00+00:00")))
        self.assertFalse(looks_like_anaphoric_reference("안녕하세요", self.store.anchors(channel="telegram", chat_id="chat-1", now="2026-08-01T08:47:00+00:00")))

    def test_long_new_deliberation_with_confirmation_word_is_not_stale_follow_up(self):
        text = (
            "얘들아, 새 canary야. 네 명이 각자 1차 의견을 내고 2차 교차검토와 "
            "3차 최종 판정까지 실제로 진행해줘. 실제 서명 결과와 delivery ack가 "
            "확인된 경우에만 통과라고 판정해"
        )
        prepared = self.store.prepare(
            channel="telegram", provider="claude", chat_id="chat-1", message_id=12,
            reply_to_message_id=None, text=text, now="2026-08-02T10:40:00+00:00",
        )
        self.assertEqual(prepared.resolution.status, "resolved")
        self.assertFalse(prepared.guard_required)
        self.assertIn("새 canary야", prepared.prompt_block)

    def test_ttl_marks_stale_without_deleting_and_reply_overrides_ttl(self):
        self.store.prepare(
            channel="telegram", provider="claude", chat_id="chat-1", message_id=20,
            reply_to_message_id=None, text="https://example.com/video", now="2026-08-01T08:44:00+00:00",
        )
        stale = self.store.resolve_anchor(
            channel="telegram", chat_id="chat-1", text="팩트체크 해줘", now="2026-08-01T09:00:01+00:00",
        )
        self.assertEqual(stale.status, "stale")
        self.assertEqual(len(self.store.anchors(channel="telegram", chat_id="chat-1", now="2026-08-01T09:00:01+00:00")), 1)
        replied = self.store.resolve_anchor(
            channel="telegram", chat_id="chat-1", text="확인해줘", reply_to_message_id=20,
            now="2026-08-01T09:00:02+00:00",
        )
        self.assertEqual(replied.status, "resolved")

    def test_multiple_recent_anchors_are_ambiguous(self):
        self.store.prepare(channel="telegram", provider="claude", chat_id="chat-1", message_id=30, reply_to_message_id=None, text="https://example.com/a", now="2026-08-01T08:44:00+00:00")
        self.store.prepare(channel="telegram", provider="claude", chat_id="chat-1", message_id=31, reply_to_message_id=None, text="https://example.com/b", now="2026-08-01T08:45:00+00:00")
        result = self.store.resolve_anchor(channel="telegram", chat_id="chat-1", text="분석해줘", now="2026-08-01T08:47:00+00:00")
        self.assertEqual(result.status, "ambiguous")

    def test_chat_isolation_and_bot_reply_binding(self):
        self.store.prepare(channel="telegram", provider="claude", chat_id="chat-a", message_id=40, reply_to_message_id=None, text="https://example.com/a", now="2026-08-01T08:44:00+00:00")
        self.store.prepare(channel="telegram", provider="claude", chat_id="chat-b", message_id=41, reply_to_message_id=None, text="https://example.com/b", now="2026-08-01T08:44:00+00:00")
        self.store.bind_response_message(channel="telegram", chat_id="chat-a", source_message_id=40, response_message_id=400)
        result = self.store.resolve_anchor(channel="telegram", chat_id="chat-a", text="확인해줘", reply_to_message_id=400, now="2026-08-02T08:44:00+00:00")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.anchor.sanitized_value, "https://example.com/a")
        other = self.store.resolve_anchor(channel="telegram", chat_id="chat-b", text="안녕하세요", reply_to_message_id=400, now="2026-08-01T08:47:00+00:00")
        self.assertEqual(other.status, "none")

    def test_sensitive_values_are_redacted_and_never_rendered(self):
        anchor = extract_anchor(
            "https://example.com/path/token=do-not-store?password=secret",
            chat_id="chat-1", source_message_id=50, now="2026-08-01T08:44:00+00:00",
        )
        self.assertEqual(anchor.sanitized_value, "[redacted]")
        prepared = self.store.prepare(channel="telegram", provider="claude", chat_id="chat-1", message_id=50, reply_to_message_id=None, text="https://example.com/path/token=do-not-store?password=secret", now="2026-08-01T08:44:00+00:00")
        self.assertNotIn("do-not-store", prepared.prompt_block)
        self.assertNotIn("secret", prepared.prompt_block)

    def test_url_userinfo_and_greetings_are_not_stored_as_context(self):
        credential = extract_anchor(
            "https://user:password@example.com/video",
            chat_id="chat-1", source_message_id=51, now="2026-08-01T08:44:00+00:00",
        )
        self.assertEqual(credential.sanitized_value, "[redacted]")
        greeting = extract_anchor(
            "안녕하세요",
            chat_id="chat-1", source_message_id=52, now="2026-08-01T08:44:00+00:00",
        )
        self.assertIsNone(greeting)

    def test_malformed_url_is_fail_safe(self):
        anchor = extract_anchor(
            "https://[",
            chat_id="chat-1", source_message_id=53, now="2026-08-01T08:44:00+00:00",
        )
        self.assertEqual(anchor.sanitized_value, "[redacted]")

    def test_read_modify_write_is_locked(self):
        def write(index):
            return self.store.save_envelope(
                prepared := __import__("edge_agent_context_envelope").ContextEnvelope(
                    SCHEMA_VERSION, "telegram", "claude", "chat-lock", str(index), None,
                    self.store.logical_session_id(channel="telegram", chat_id="chat-lock"),
                    f"2026-08-01T08:{44 + index // 60:02d}:{index % 60:02d}+00:00", str(index),
                ),
                EntityAnchor("chat-lock", str(index), "topic", f"topic-{index}", 0.35, f"2026-08-01T08:44:{index:02d}+00:00"),
            )
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(write, range(8))))
        self.assertEqual(len(self.store.anchors(channel="telegram", chat_id="chat-lock", now="2026-08-01T08:50:00+00:00")), 8)

    def test_chat_specific_native_paths_and_legacy_path(self):
        base = Path(self.tempdir.name) / "claude.json"
        self.assertNotEqual(native_session_path(base, role="claude", chat_id="a"), native_session_path(base, role="claude", chat_id="b"))
        self.assertEqual(native_session_path(base, role="claude"), base.resolve())

    def test_native_resume_rejects_workspace_metadata_mismatch(self):
        workspace = Path(self.tempdir.name) / "workspace"
        workspace.mkdir()
        session_file = Path(self.tempdir.name) / "native.json"
        with patch.dict(os.environ, {"TELEGRAM_AGENT_NATIVE_SESSION_FILE": str(session_file)}):
            self.bot._persist_native_session_id("claude", "12345678-1234-5678-1234-567812345678", chat_id="chat-1", workspace=workspace)
            self.assertEqual(self.bot._load_native_session_id("claude", chat_id="chat-1", workspace=workspace), "12345678-1234-5678-1234-567812345678")
            path = self.bot._native_session_path("claude", "chat-1")
            payload = __import__("json").loads(path.read_text(encoding="utf-8"))
            payload["workspace_identity"] = "wrong"
            path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            self.assertIsNone(self.bot._load_native_session_id("claude", chat_id="chat-1", workspace=workspace))

    def test_native_session_budget_rotates_before_resume(self):
        workspace = Path(self.tempdir.name) / "workspace"
        workspace.mkdir()
        session_id = "12345678-1234-5678-1234-567812345678"
        with patch.dict(os.environ, {"TELEGRAM_AGENT_NATIVE_SESSION_FILE": str(Path(self.tempdir.name) / "native.json")}):
            self.bot._persist_native_session_id(
                "claude",
                session_id,
                chat_id="chat-budget",
                workspace=workspace,
                turn_count=self.bot.CLAUDE_NATIVE_MAX_TURNS,
                prompt_chars=0,
            )
            record = self.bot._load_native_session_record("claude", chat_id="chat-budget", workspace=workspace)
            self.assertTrue(self.bot._native_session_budget_exceeded(record, 1))

    def test_malformed_native_session_payload_is_ignored(self):
        workspace = Path(self.tempdir.name) / "workspace"
        workspace.mkdir()
        session_file = Path(self.tempdir.name) / "native.json"
        with patch.dict(os.environ, {"TELEGRAM_AGENT_NATIVE_SESSION_FILE": str(session_file)}):
            session_file.write_text("[]", encoding="utf-8")
            self.assertIsNone(self.bot._load_native_session_record("claude", chat_id="chat-bad", workspace=workspace))

    def test_runtime_prompt_can_render_a_different_provider_identity(self):
        runtime_prompt, _ = self.bot._runtime_prompt_parts(
            "코드 리뷰",
            role="codex",
            workspace=self.bot.CODEX_WORKSPACE,
        )
        self.assertIn("Codex", runtime_prompt)
        self.assertNotIn("Antigravity (안티)", runtime_prompt)


class AdapterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ContextEnvelopeStore(self.tempdir.name)
        os.environ["TELEGRAM_AGENT_ROLE"] = "claude"
        os.environ["TELEGRAM_AGENT_CHAT_ID"] = "-1003952617795"
        os.environ["TELEGRAM_AGENT_TOKEN_FILE"] = str(Path.home() / ".config" / "agent-telegram" / "claude.token")
        self.bot = load_module("telegram_agent_bot_context_test", "telegram-agent-bot.py")
        self.roda_token = Path(self.tempdir.name) / "roda.token"
        self.roda_token.write_text("test-token\n", encoding="utf-8")
        self.roda_token.chmod(0o600)
        os.environ["RODA_GEMMA_TOKEN_FILE"] = str(self.roda_token)
        os.environ["RODA_GEMMA_ALLOWED_USER_IDS"] = "6417205500"
        os.environ["RODA_GEMMA_ALLOWED_GROUP_IDS"] = "-1003952617795"
        self.roda = load_module("roda_gemma_bot_context_test", "roda-gemma-bot.py")

    def tearDown(self):
        self.tempdir.cleanup()

    def update(self, text: str, message_id: int = 100, reply_to=None):
        sent_counter = {"value": 800}

        async def reply_text(value):
            sent_counter["value"] += 1
            return FakeSent(sent_counter["value"])

        message = SimpleNamespace(
            text=text, caption=None, entities=None, caption_entities=None,
            from_user=SimpleNamespace(is_bot=False, id=1), chat_id=-1003952617795,
            message_id=message_id, reply_to_message=reply_to, date=datetime.now(timezone.utc),
            reply_text=reply_text,
        )
        chat = SimpleNamespace(type=self.bot.ChatType.GROUP, id=-1003952617795)
        return SimpleNamespace(effective_message=message, effective_chat=chat)

    async def test_handle_message_injects_anchor_context_for_all_provider_roles(self):
        captured = []
        async def fake_provider(prompt, **kwargs):
            captured.append(kwargs["context_prompt"])
            return "ok"

        for role, addressed in (("claude", "클로드야"), ("codex", "코덱스야"), ("antigravity", "안티야")):
            with self.subTest(role=role), patch.dict(os.environ, {"TELEGRAM_CONTEXT_ROOT": self.tempdir.name}), \
                    patch.object(self.bot, "ROLE", role), patch.object(self.bot, "run_provider", side_effect=fake_provider), \
                    patch.object(self.bot, "write_task_state", return_value=f"task-{role}"), \
                    patch.object(self.bot, "start_session", return_value=f"sess-{role}"), \
                    patch.object(self.bot, "update_session"), patch.object(self.bot, "write_reflection"), \
                    patch.object(self.bot, "_record_telegram_efficiency"), patch.object(self.bot, "_needs_task_worktree", return_value=False):
                self.bot.ACTIVE_TASK_WORKSPACE = None
                self.bot.ACTIVE_LOGICAL_SESSION_ID = None
                await self.bot.handle_message(self.update(f"{addressed} 팩트체크 https://example.com/{role}"), SimpleNamespace())
        self.assertEqual(len(captured), 3)
        for prompt in captured:
            self.assertIn("https://example.com", prompt)
            self.assertIn("sanitized anchor", prompt)

    def test_shared_team_contract_is_loaded_by_direct_and_roda_bridges(self):
        direct_prompt, _ = self.bot._runtime_prompt_parts("모두 자기 소개 부탁해")
        self.assertIn("공통 Edge Agent Telegram 팀 계약", direct_prompt)
        self.assertIn("Roda", direct_prompt)
        self.assertIn("공통 Edge Agent Telegram 팀 계약", self.roda.TEAM_CONTRACT)
        self.assertIn("Edge Agent Telegram 팀의 Roda 구성원", self.roda.SYSTEM_PROMPT)

    async def test_ambiguous_context_blocks_provider(self):
        self.store.prepare(channel="telegram", provider="claude", chat_id="-1003952617795", message_id=1, reply_to_message_id=None, text="https://example.com/a", now="2026-08-01T08:44:00+00:00")
        self.store.prepare(channel="telegram", provider="claude", chat_id="-1003952617795", message_id=2, reply_to_message_id=None, text="https://example.com/b", now="2026-08-01T08:45:00+00:00")
        with patch.dict(os.environ, {"TELEGRAM_CONTEXT_ROOT": self.tempdir.name}), \
                patch.object(self.bot, "ROLE", "claude"), patch.object(self.bot, "run_provider", new=AsyncMock(side_effect=AssertionError("provider must not run"))), \
                patch.object(self.bot, "write_task_state", return_value="task-ambiguous"), \
                patch.object(self.bot, "start_session", return_value="sess-ambiguous"), \
                patch.object(self.bot, "update_session"), patch.object(self.bot, "_is_stale", return_value=False):
            await self.bot.handle_message(self.update("클로드야 분석해줘", message_id=3), SimpleNamespace())

    async def test_roda_handle_injects_context_and_blocks_ambiguous_anchor(self):
        captured = []
        async def reply_text(value):
            return FakeSent(900)

        async def send_chat_action(**kwargs):
            return None

        def run_roda(text):
            captured.append(text)
            return "ok"

        message = SimpleNamespace(
            text="@sukja_hwpx_helper_bot https://example.com/roda",
            caption=None, message_id=70, reply_to_message=None,
            reply_text=reply_text,
        )
        chat = SimpleNamespace(type=self.roda.ChatType.GROUP, id=-1003952617795)
        user = SimpleNamespace(id=6417205500)
        update = SimpleNamespace(effective_message=message, effective_chat=chat, effective_user=user)
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=send_chat_action))
        with patch.dict(os.environ, {"TELEGRAM_CONTEXT_ROOT": self.tempdir.name}), patch.object(self.roda, "_ollama_chat", side_effect=run_roda), patch.object(self.roda, "_context_store", return_value=self.store):
            await self.roda.handle_message(update, context)
        self.assertEqual(len(captured), 1)
        self.assertIn("https://example.com/roda", captured[0])
        self.assertIn("Telegram context envelope", captured[0])

        self.store.prepare(channel="telegram", provider="gemma", chat_id="-1003952617795", message_id=71, reply_to_message_id=None, text="https://example.com/a", now="2026-08-01T08:44:00+00:00")
        self.store.prepare(channel="telegram", provider="gemma", chat_id="-1003952617795", message_id=72, reply_to_message_id=None, text="https://example.com/b", now="2026-08-01T08:45:00+00:00")
        message.text = "@sukja_hwpx_helper_bot 팩트체크 해줘"
        message.message_id = 73
        with patch.dict(os.environ, {"TELEGRAM_CONTEXT_ROOT": self.tempdir.name}), patch.object(self.roda, "_ollama_chat", side_effect=AssertionError("Ollama must not run")), patch.object(self.roda, "_context_store", return_value=self.store):
            await self.roda.handle_message(update, context)
        self.assertEqual(len(captured), 1)

    async def test_simple_meeting_peers_are_internal_and_codex_publishes_once(self):
        sent = []
        provider_calls = []

        class MeetingStore:
            results = {"roda": "사용자 관점 의견"}

            def start(self, session_id, request, *, roles, mode):
                self.mode = mode
                return {"mode": mode}

            def record(self, session_id, role, *, status, summary, **kwargs):
                if status == "completed":
                    self.results[role] = summary
                return {"status": "collecting"}

            def wait_for_round(self, *args, **kwargs):
                return {"status": "barrier_ready"}

            def render_conversation(self, session_id):
                return "\n".join(f"- {role}: {summary}" for role, summary in sorted(self.results.items()))

        store = MeetingStore()

        async def fake_provider(prompt, **kwargs):
            provider_calls.append((self.bot.ROLE, kwargs))
            if self.bot.ROLE == "codex" and "회의 사회자 통합" in kwargs.get("provider_text", ""):
                return "세 의견을 들은 통합 결론"
            return f"{self.bot.ROLE}의 독립 의견"

        async def fake_egress(chat_id, delivery_id, chunk_index, sender):
            return await sender()

        def fake_delivery(**kwargs):
            return {"delivery_id": "delivery-1"}

        def meeting_update():
            async def reply_text(value):
                sent.append(value)
                return FakeSent(950 + len(sent))

            message = SimpleNamespace(
                text="얘들아 현재 시스템 장단점을 회의하고 하나의 결론으로 통합해줘",
                caption=None,
                entities=None,
                caption_entities=None,
                from_user=SimpleNamespace(is_bot=False, id=1),
                chat_id=-1003952617795,
                message_id=901,
                reply_to_message=None,
                date=datetime.now(timezone.utc),
                reply_text=reply_text,
            )
            return SimpleNamespace(
                effective_message=message,
                effective_chat=SimpleNamespace(type=self.bot.ChatType.GROUP, id=-1003952617795),
            )

        common = (
            patch.object(self.bot, "SIMPLE_MEETING_MODE", True),
            patch.object(self.bot, "DeliberationStore", return_value=store),
            patch.object(self.bot, "run_provider", side_effect=fake_provider),
            patch.object(self.bot, "_prepare_context", return_value=None),
            patch.object(self.bot, "_ingress_identity", return_value=None),
            patch.object(self.bot, "_is_stale", return_value=False),
            patch.object(self.bot, "_observe_shadow_update"),
            patch.object(self.bot, "write_task_state", side_effect=lambda **kwargs: kwargs.get("task_id") or f"task-{self.bot.ROLE}"),
            patch.object(self.bot, "start_session", side_effect=lambda **kwargs: f"session-{self.bot.ROLE}"),
            patch.object(self.bot, "update_session"),
            patch.object(self.bot, "write_reflection"),
            patch.object(self.bot, "_record_telegram_efficiency"),
            patch.object(self.bot, "_update_task_worktree_status"),
            patch.object(self.bot, "create_delivery", side_effect=fake_delivery),
            patch.object(self.bot, "mark_chunk_sent"),
            patch.object(self.bot, "mark_delivery_succeeded"),
            patch.object(self.bot, "_egress_send", side_effect=fake_egress),
            patch.object(self.bot._CONTROL_PLANE, "start_task"),
            patch.object(self.bot._CONTROL_PLANE, "mark_task"),
        )
        with ExitStack() as stack:
            for manager in common:
                stack.enter_context(manager)
            for role in ("claude", "antigravity", "codex"):
                with patch.object(self.bot, "ROLE", role):
                    self.bot.ACTIVE_TASK_WORKSPACE = None
                    self.bot.ACTIVE_LOGICAL_SESSION_ID = None
                    await self.bot.handle_message(meeting_update(), SimpleNamespace())

        self.assertEqual(sent, ["세 의견을 들은 통합 결론"])
        self.assertEqual([role for role, _ in provider_calls], ["claude", "antigravity", "codex", "codex"])
        self.assertTrue(all(kwargs.get("conversation_meeting") is True for _, kwargs in provider_calls))
        self.assertEqual(store.mode, "conversation")

    async def test_roda_simple_meeting_submits_internal_opinion_without_reply(self):
        sent = []
        observed = []

        class MeetingStore:
            def start(self, session_id, request, *, roles, mode):
                observed.append(("mode", mode))
                return {"mode": mode}

            def record(self, session_id, role, *, status, summary, **kwargs):
                observed.append((role, status, summary))
                return {"status": "collecting"}

        async def reply_text(value):
            sent.append(value)
            return FakeSent(990)

        async def send_chat_action(**kwargs):
            return None

        def run_roda(text, *, conversation_meeting=False):
            observed.append(("provider", conversation_meeting))
            return "Roda의 사용자 관점 의견"

        message = SimpleNamespace(
            text="얘들아 현재 시스템 장단점을 회의하고 하나의 결론으로 통합해줘",
            caption=None,
            message_id=902,
            reply_to_message=None,
            reply_text=reply_text,
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(type=self.roda.ChatType.GROUP, id=-1003952617795),
            effective_user=SimpleNamespace(id=6417205500),
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=send_chat_action))
        with patch.object(self.roda, "SIMPLE_MEETING_MODE", True), \
                patch.object(self.roda, "DeliberationStore", return_value=MeetingStore()), \
                patch.object(self.roda, "_ollama_chat", side_effect=run_roda), \
                patch.object(self.roda, "_context_store", return_value=self.store), \
                patch.object(self.roda, "write_task_state", return_value="task-roda"), \
                patch.object(self.roda._CONTROL_PLANE, "start_task"), \
                patch.object(self.roda._CONTROL_PLANE, "mark_task"):
            await self.roda.handle_message(update, context)

        self.assertEqual(sent, [])
        self.assertIn(("mode", "conversation"), observed)
        self.assertIn(("provider", True), observed)
        self.assertIn(("roda", "completed", "Roda의 사용자 관점 의견"), observed)

    async def test_private_chats_never_enter_conversation_meeting_mode(self):
        meeting_text = "현재 시스템 장단점을 회의하고 하나의 결론으로 통합해줘"
        direct_update = self.update(meeting_text, message_id=903)
        direct_update.effective_chat.type = self.bot.ChatType.PRIVATE
        direct_provider = AsyncMock(return_value="일반 답변")

        with patch.object(self.bot, "addressed_text", return_value=meeting_text), \
                patch.object(self.bot, "SIMPLE_MEETING_MODE", True), \
                patch.object(self.bot, "is_conversation_meeting", return_value=True) as direct_detector, \
                patch.object(self.bot, "is_deliberation_request", return_value=False), \
                patch.object(self.bot, "run_provider", new=direct_provider), \
                patch.object(self.bot, "_prepare_context", return_value=None), \
                patch.object(self.bot, "_ingress_identity", return_value=None), \
                patch.object(self.bot, "_is_stale", return_value=False), \
                patch.object(self.bot, "_needs_task_worktree", return_value=False), \
                patch.object(self.bot, "write_task_state", return_value="task-private"), \
                patch.object(self.bot, "start_session", return_value="session-private"), \
                patch.object(self.bot, "update_session"), \
                patch.object(self.bot, "write_reflection"), \
                patch.object(self.bot, "_record_telegram_efficiency"), \
                patch.object(self.bot, "_update_task_worktree_status"), \
                patch.object(self.bot._CONTROL_PLANE, "start_task"), \
                patch.object(self.bot._CONTROL_PLANE, "mark_task"):
            self.bot.ACTIVE_TASK_WORKSPACE = None
            self.bot.ACTIVE_LOGICAL_SESSION_ID = None
            await self.bot.handle_message(direct_update, SimpleNamespace())

        direct_detector.assert_not_called()
        self.assertIs(direct_provider.await_args.kwargs.get("conversation_meeting", False), False)

        roda_calls = []

        async def reply_text(value):
            return FakeSent(991)

        async def send_chat_action(**kwargs):
            return None

        def run_roda(text, **kwargs):
            roda_calls.append(kwargs)
            return "일반 답변"

        roda_update = SimpleNamespace(
            effective_message=SimpleNamespace(
                text=meeting_text,
                caption=None,
                message_id=904,
                reply_to_message=None,
                reply_text=reply_text,
            ),
            effective_chat=SimpleNamespace(type=self.roda.ChatType.PRIVATE, id=6417205500),
            effective_user=SimpleNamespace(id=6417205500),
        )
        roda_context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=send_chat_action))
        with patch.object(self.roda, "SIMPLE_MEETING_MODE", True), \
                patch.object(self.roda, "is_conversation_meeting", return_value=True) as roda_detector, \
                patch.object(self.roda, "_ollama_chat", side_effect=run_roda), \
                patch.object(self.roda, "_context_store", return_value=self.store), \
                patch.object(self.roda, "write_task_state", return_value="task-roda-private"), \
                patch.object(self.roda._CONTROL_PLANE, "start_task"), \
                patch.object(self.roda._CONTROL_PLANE, "mark_task"):
            await self.roda.handle_message(roda_update, roda_context)

        roda_detector.assert_not_called()
        self.assertEqual(roda_calls, [{}])


if __name__ == "__main__":
    unittest.main()
