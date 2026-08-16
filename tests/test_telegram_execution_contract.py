#!/usr/bin/env python3
"""Focused tests for Telegram execution handoff and worktree ownership."""

from __future__ import annotations

import importlib.util
import ast
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
from edge_agent_delegation import DelegationStore, delegation_id_for  # noqa: E402
from edge_agent_telegram_delivery import create_delivery, load_delivery  # noqa: E402
_ENV_KEYS = ("TELEGRAM_AGENT_ROLE", "TELEGRAM_AGENT_CHAT_ID", "TELEGRAM_AGENT_TOKEN_FILE")
_ENV_SNAPSHOT = {key: os.environ.get(key) for key in _ENV_KEYS}
_TEST_TOKEN_ROOT = tempfile.TemporaryDirectory()
_TEST_TOKEN_FILE = Path(_TEST_TOKEN_ROOT.name) / "claude.token"
_TEST_TOKEN_FILE.write_text("123456:unit-test-token", encoding="utf-8")
_TEST_TOKEN_FILE.chmod(0o600)
_BOT_RUNTIME_HOME_ROOT = tempfile.TemporaryDirectory()
os.environ["TELEGRAM_AGENT_ROLE"] = "claude"
os.environ["TELEGRAM_AGENT_CHAT_ID"] = "-1003952617795"
os.environ["TELEGRAM_AGENT_TOKEN_FILE"] = str(_TEST_TOKEN_FILE)
SPEC = importlib.util.spec_from_file_location("telegram_agent_bot_execution_contract", BIN / "telegram-agent-bot.py")
BOT = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
with patch.object(Path, "home", return_value=Path(_BOT_RUNTIME_HOME_ROOT.name)):
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
    def test_provider_auth_failure_is_safe_and_actionable(self):
        message = BOT._provider_failure_message(
            "claude",
            "Failed to authenticate: OAuth session expired and could not be refreshed",
            "",
        )
        self.assertIn("OAuth 인증이 만료", message)
        self.assertNotIn("로그를 확인", message)
        self.assertIn("로그를 확인", BOT._provider_failure_message("antigravity", "", "exit 1"))

    def test_delegated_review_turn_limit_is_bounded(self):
        self.assertGreaterEqual(BOT.CLAUDE_DELEGATED_REVIEW_MAX_TURNS, 2)
        self.assertLessEqual(BOT.CLAUDE_DELEGATED_REVIEW_MAX_TURNS, 8)

    def setUp(self):
        self.delivery_root = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.delivery_root.cleanup()

    def test_ingress_identity_is_message_stable_and_role_scoped(self):
        message = SimpleNamespace(chat_id=-1003952617795, message_id=42)
        update = SimpleNamespace(
            update_id=100,
            effective_chat=SimpleNamespace(type=BOT.ChatType.GROUP, id=message.chat_id),
        )
        with patch.object(BOT, "ROLE", "claude"):
            claude_task, claude_key, root = BOT._ingress_identity(update, message, bot_id="claude")
        with patch.object(BOT, "ROLE", "antigravity"):
            agy_task, agy_key, agy_root = BOT._ingress_identity(update, message, bot_id="antigravity")
        self.assertEqual(root, agy_root)
        self.assertEqual(claude_key, agy_key)
        self.assertNotEqual(claude_task, agy_task)

    def test_live_router_produces_risk_and_worktree_metadata(self):
        decision = BOT.deterministic_route(BOT.RouterInput("코드 수정 후 검토해줘"))
        self.assertTrue(decision.requires_worktree)
        self.assertTrue(decision.requires_approval)
        self.assertIn("task_type:coding", decision.reason_codes)

    def test_conflict_burst_persists_a_bounded_restart_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            original_file = BOT._CONFLICT_COOLDOWN_FILE
            original_seconds = BOT.CONFLICT_RESTART_COOLDOWN_SECONDS
            BOT._CONFLICT_COOLDOWN_FILE = Path(directory) / "cooldown.json"
            BOT.CONFLICT_RESTART_COOLDOWN_SECONDS = 60
            try:
                with patch.object(BOT.time, "time", return_value=100):
                    BOT._write_conflict_cooldown()
                self.assertTrue(BOT._CONFLICT_COOLDOWN_FILE.is_file())
                with patch.object(BOT.time, "time", return_value=101), patch.object(BOT.time, "sleep") as sleep:
                    BOT._wait_for_conflict_cooldown()
                sleep.assert_called_once()
                self.assertLessEqual(sleep.call_args.args[0], 60)
                BOT._clear_conflict_cooldown()
                self.assertFalse(BOT._CONFLICT_COOLDOWN_FILE.exists())
            finally:
                BOT._CONFLICT_COOLDOWN_FILE = original_file
                BOT.CONFLICT_RESTART_COOLDOWN_SECONDS = original_seconds

    def test_singleton_lock_is_acquired_before_polling_application(self):
        tree = ast.parse((BIN / "telegram-agent-bot.py").read_text(encoding="utf-8"))
        lock_line = next(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_acquire_singleton_lock"
        )
        builder_line = next(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "builder"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Application"
        )
        self.assertLess(lock_line, builder_line)

    def test_singleton_lock_startup_path_executes_without_unresolved_dependencies(self):
        original_directory = BOT._SINGLETON_LOCK_DIR
        original_fd = BOT._singleton_lock_fd
        with tempfile.TemporaryDirectory() as directory:
            BOT._SINGLETON_LOCK_DIR = Path(directory)
            BOT._singleton_lock_fd = None
            try:
                BOT._acquire_singleton_lock()
                self.assertIsInstance(BOT._singleton_lock_fd, int)
            finally:
                if BOT._singleton_lock_fd is not None:
                    os.close(BOT._singleton_lock_fd)
                BOT._singleton_lock_fd = original_fd
                BOT._SINGLETON_LOCK_DIR = original_directory

    def test_provider_permission_contract_does_not_use_global_bypass(self):
        source = (BIN / "telegram-agent-bot.py").read_text(encoding="utf-8")
        self.assertNotIn("--dangerously-skip-permissions", source)
        self.assertIn('"--permission-mode", "acceptEdits"', source)
        self.assertIn('"plan" if conversation_meeting else "accept-edits"', source)

    async def test_conversation_meeting_uses_plan_mode_for_claude_and_antigravity(self):
        commands = []

        class Unlocked:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class CompletedProcess:
            pid = os.getpid()
            returncode = 0

            async def communicate(self):
                return b"meeting opinion", b""

        async def spawn(*args, **kwargs):
            commands.append(args)
            return CompletedProcess()

        test_roles = {
            role: {**BOT.ROLES[role], "binary": Path(sys.executable)}
            for role in ("claude", "antigravity")
        }
        with patch.dict(BOT.ROLES, test_roles), \
                patch.object(BOT, "acquire_workspace_lock", return_value=Unlocked()), \
                patch.object(BOT.asyncio, "create_subprocess_exec", side_effect=spawn), \
                patch.object(BOT, "_load_native_session_record", return_value=None), \
                patch.object(BOT, "_persist_native_session_id"):
            await BOT._run_cli("claude", "의견", conversation_meeting=True)
            await BOT._run_cli("antigravity", "의견", conversation_meeting=True)

        claude_args, antigravity_args = commands
        self.assertEqual(claude_args[claude_args.index("--permission-mode") + 1], "plan")
        self.assertEqual(antigravity_args[antigravity_args.index("--mode") + 1], "plan")

    async def test_legacy_meeting_coordination_forwards_read_only_state(self):
        provider = AsyncMock(return_value="통합 결론")
        sent = FakeSent(11)
        update = make_update(sent)
        with patch.object(BOT, "addressed_text", return_value="논의하고 의견을 통합해줘"), \
                patch.object(BOT, "SIMPLE_MEETING_MODE", True), \
                patch.object(BOT, "is_conversation_meeting", return_value=True), \
                patch.object(BOT, "is_deliberation_request", return_value=False), \
                patch.object(BOT, "_prepare_context", return_value=None), \
                patch.object(BOT, "_ingress_identity", return_value=None), \
                patch.object(BOT, "_is_stale", return_value=False), \
                patch.object(BOT, "_needs_task_worktree", return_value=False), \
                patch.object(BOT, "wait_for_peer_results", new=AsyncMock(return_value="peer opinions")), \
                patch.object(BOT, "run_provider", new=provider), \
                patch.object(BOT, "write_task_state", return_value="task-meeting"), \
                patch.object(BOT, "start_session", return_value="session-meeting"), \
                patch.object(BOT, "update_session"), \
                patch.object(BOT, "write_reflection"), \
                patch.object(BOT, "_record_telegram_efficiency"), \
                patch.object(BOT, "_update_task_worktree_status"), \
                patch.dict(os.environ, {"EDGE_AGENT_TELEGRAM_DELIVERY_ROOT": self.delivery_root.name}), \
                patch.object(BOT, "ROLE", "codex"):
            BOT.ACTIVE_TASK_WORKSPACE = None
            BOT.ACTIVE_LOGICAL_SESSION_ID = None
            await BOT.handle_message(update, SimpleNamespace())

        provider.assert_awaited_once()
        self.assertIs(provider.await_args.kwargs["conversation_meeting"], True)

    async def test_active_meeting_interjection_appends_note_without_new_session(self):
        provider = AsyncMock(return_value="이건 절대 호출되면 안 됨")
        sent = FakeSent(12)
        update = make_update(sent)
        session_id = BOT.session_id_for_telegram(update.effective_chat.id, 999)
        with tempfile.TemporaryDirectory() as directory:
            store = BOT.DeliberationStore(directory)
            store.start(session_id, "먼저 시작된 회의")
            store.record_active_chat_session(update.effective_chat.id, session_id)
            with patch.object(BOT, "addressed_text", return_value="아 잠깐만 이것도 고려해줘"), \
                    patch.object(BOT, "DeliberationStore", return_value=store), \
                    patch.object(BOT, "_ingress_identity", return_value=None), \
                    patch.object(BOT, "_is_stale", return_value=False), \
                    patch.object(BOT, "run_provider", new=provider), \
                    patch.object(BOT, "ROLE", "codex"):
                await BOT.handle_message(update, SimpleNamespace())
            provider.assert_not_awaited()
            notes = store.snapshot(session_id)["human_notes"]
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0]["text"], "아 잠깐만 이것도 고려해줘")

    async def test_no_active_session_still_opens_new_meeting_classification(self):
        provider = AsyncMock(return_value="회의 1차 의견")
        sent = FakeSent(13)
        update = make_update(sent)
        with tempfile.TemporaryDirectory() as directory:
            store = BOT.DeliberationStore(directory)
            with patch.object(BOT, "addressed_text", return_value="논의하고 의견을 통합해줘"), \
                    patch.object(BOT, "DeliberationStore", return_value=store), \
                    patch.object(BOT, "SIMPLE_MEETING_MODE", True), \
                    patch.object(BOT, "is_conversation_meeting", return_value=True), \
                    patch.object(BOT, "_prepare_context", return_value=None), \
                    patch.object(BOT, "_ingress_identity", return_value=None), \
                    patch.object(BOT, "_is_stale", return_value=False), \
                    patch.object(BOT, "_needs_task_worktree", return_value=False), \
                    patch.object(BOT, "wait_for_peer_results", new=AsyncMock(return_value="peer opinions")), \
                    patch.object(BOT, "run_provider", new=provider), \
                    patch.object(BOT, "write_task_state", return_value="task-meeting-2"), \
                    patch.object(BOT, "start_session", return_value="session-meeting-2"), \
                    patch.object(BOT, "update_session"), \
                    patch.object(BOT, "write_reflection"), \
                    patch.object(BOT, "_record_telegram_efficiency"), \
                    patch.object(BOT, "_update_task_worktree_status"), \
                    patch.dict(os.environ, {"EDGE_AGENT_TELEGRAM_DELIVERY_ROOT": self.delivery_root.name}), \
                    patch.object(BOT, "ROLE", "codex"):
                BOT.ACTIVE_TASK_WORKSPACE = None
                BOT.ACTIVE_LOGICAL_SESSION_ID = None
                await BOT.handle_message(update, SimpleNamespace())
        provider.assert_awaited_once()

    async def test_coordinator_reintegrates_late_human_note_before_final_synthesis(self):
        provider_outputs = iter([
            "claude 1차 의견",
            "claude 2차 의견",
            "claude 3차 의견",
            "최초 최종 종합",
            "늦은 발언까지 반영한 최종 종합",
        ])
        sent = FakeSent(14)
        update = make_update(sent)
        update.effective_message.message_id = 230
        session_id = BOT.session_id_for_telegram(update.effective_chat.id, 230)
        task_states = []
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            store = BOT.DeliberationStore(directory)

            async def provider(*args, **kwargs):
                output = next(provider_outputs)
                if output == "claude 1차 의견":
                    store.append_human_note(
                        session_id,
                        "2차부터 반영할 발언",
                        telegram_message_id=229,
                    )
                return output

            provider_mock = AsyncMock(side_effect=provider)

            def complete_round(current_session_id, round_number):
                for role in ("codex", "antigravity", "roda"):
                    store.record(
                        current_session_id,
                        role,
                        status="completed",
                        summary=f"{role} {round_number}차 의견",
                        round_number=round_number,
                    )
                if round_number == 3:
                    store.append_human_note(
                        current_session_id,
                        "최종 종합 직전의 늦은 발언",
                        telegram_message_id=231,
                    )

            with patch.dict("os.environ", {
                    "EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path),
                    "EDGE_AGENT_TELEGRAM_DELIVERY_ROOT": self.delivery_root.name,
                }), \
                    patch.object(BOT, "addressed_text", return_value="장단점을 회의하고 최종 의견을 통합해줘"), \
                    patch.object(BOT, "DeliberationStore", return_value=store), \
                    patch.multiple(
                        BOT,
                        SIMPLE_MEETING_MODE=False,
                        is_deliberation_request=lambda text: True,
                        classify_ingress=lambda text: SimpleNamespace(accepts=lambda role: True),
                        roles_for_request=lambda text: ("claude", "codex", "antigravity", "roda"),
                    ), \
                    patch.object(BOT, "_prepare_context", return_value=None), \
                    patch.object(BOT, "_ingress_identity", return_value=None), \
                    patch.object(BOT, "_is_stale", return_value=False), \
                    patch.object(BOT, "_needs_task_worktree", return_value=False), \
                    patch.object(BOT, "_require_deliberation_round", side_effect=complete_round), \
                    patch.object(BOT, "run_provider", new=provider_mock), \
                    patch.object(
                        BOT,
                        "write_task_state",
                        side_effect=lambda **kwargs: task_states.append(kwargs) or "task-meeting-reintegration",
                    ), \
                    patch.object(BOT, "start_session", return_value="session-meeting-reintegration"), \
                    patch.object(BOT, "update_session"), \
                    patch.object(BOT, "write_reflection"), \
                    patch.object(BOT, "_record_telegram_efficiency"), \
                    patch.object(BOT, "_update_task_worktree_status"), \
                    patch.object(BOT, "ROLE", "claude"):
                BOT.ACTIVE_TASK_WORKSPACE = None
                BOT.ACTIVE_LOGICAL_SESSION_ID = None
                await BOT.handle_message(update, SimpleNamespace())

            self.assertEqual(provider_mock.await_count, 5)
            reintegration_prompt = provider_mock.await_args_list[-1].kwargs["provider_text"]
            self.assertIn("새로 도착한 사람 발언을 반드시 반영", reintegration_prompt)
            self.assertIn("최종 종합 직전의 늦은 발언", reintegration_prompt)
            snapshot = store.snapshot(session_id)
            self.assertEqual(snapshot["results"]["claude"]["observed_human_seq"], 0)
            self.assertEqual(snapshot["human_note_reintegrations"], 1)
            self.assertIs(snapshot["human_notes_closed"], True)
            completed_state = next(item for item in reversed(task_states) if item["status"] == "completed")
            self.assertNotIn("추가 의견은 다음 회의에서 다룹니다", completed_state["response_preview"])

    def test_delegation_delivery_uses_defined_reply_chunker(self):
        source = (BIN / "telegram-agent-bot.py").read_text(encoding="utf-8")
        self.assertNotIn("for part in _split_message(answer)", source)
        self.assertIn("for part in _reply_chunks(answer)", source)
        chunks = BOT._reply_chunks("x" * (BOT.CHUNK_SIZE + 1))
        self.assertEqual([len(chunk) for chunk in chunks], [BOT.CHUNK_SIZE, 1])

    def test_claude_session_limit_is_probed_with_a_fresh_nonpersistent_session(self):
        self.assertTrue(BOT._is_claude_session_local_failure("", "You've hit your session limit · resets 7pm"))
        self.assertFalse(BOT._is_claude_session_local_failure("", "account authentication failed"))
        original = ["claude", "-p", "--resume", "old-session", "--output-format", "text", "--", "prompt"]
        retry = BOT._fresh_claude_retry_args(original, "fresh-session")
        self.assertIn("--no-session-persistence", retry)
        self.assertIn("--session-id", retry)
        self.assertNotIn("--resume", retry)
        self.assertIn("fresh-session", retry)

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

    async def test_antigravity_delegation_delivers_and_completes(self):
        class FakeBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, **kwargs):
                self.messages.append(kwargs)
                return SimpleNamespace(message_id=len(self.messages))

        with tempfile.TemporaryDirectory() as directory:
            store = DelegationStore(directory)
            delegation_id = delegation_id_for(root_task_id="telegram-test", target_role="antigravity")
            payload = store.create(
                delegation_id,
                root_task_id="telegram-test",
                source_role="codex",
                target_role="antigravity",
                chat_id="-100",
                reply_to_message_id=7,
                request="돌핀 태풍의 진행 상황 알려줘",
                reason="공개 자료 조사",
                acceptance_criteria="출처와 조회 시각 포함",
            )
            fake_bot = FakeBot()
            application = SimpleNamespace(bot=fake_bot)
            with patch.object(BOT, "DelegationStore", return_value=store), \
                    patch.object(BOT, "ROLE", "antigravity"), \
                    patch.object(BOT, "run_provider", new=AsyncMock(return_value="관측된 검색 결과")), \
                    patch.object(BOT, "write_task_state"):
                await BOT._process_delegation(application, payload)

            result = store.snapshot(delegation_id)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["response"], "관측된 검색 결과")
            self.assertEqual(len(fake_bot.messages), 2)

    async def test_claude_coding_delegation_uses_independent_verification_loop(self):
        with patch.object(BOT, "codex_verify_and_revise", new=AsyncMock(return_value="verified")) as verify:
            result = await BOT.claude_delegates_to_codex("수정해줘", object(), chat_id="chat-1")
        verify.assert_awaited_once()
        self.assertIn("독립 검증한 결과", result)

    async def test_verification_loop_calls_codex_explicitly_and_isolates_claude_review(self):
        class ReplyMessage:
            def __init__(self):
                self.replies = []

            async def reply_text(self, text):
                self.replies.append(text)

        message = ReplyMessage()
        with tempfile.TemporaryDirectory() as directory:
            task_root = Path(directory)
            task_workspace = task_root / "task-1"
            task_workspace.mkdir()
            (task_workspace / ".edge-agent-task.json").write_text(json.dumps({
                "schema": "edge_agent_worktree.v1",
                "task_id": "task-1",
                "role": "claude",
            }), encoding="utf-8")
            with patch.object(BOT, "ACTIVE_TASK_WORKSPACE", task_workspace), \
                    patch.object(BOT, "CODEX_TASK_WORKTREE_ROOT", task_root), \
                    patch.object(BOT, "run_provider_as", new=AsyncMock(return_value="codex changed the workspace")) as codex, \
                    patch.object(BOT, "_run_cli", new=AsyncMock(side_effect=["RESULT: PASS", "RESULT: PASS"])) as reviewers, \
                    patch.object(BOT, "CODEX_VERIFY_MAX_ROUNDS", 1), \
                    patch.object(BOT, "CODEX_VERIFY_TOTAL_TIMEOUT_SECONDS", 60), \
                    patch.object(BOT, "CODEX_VERIFY_CALL_TIMEOUT_SECONDS", 30):
                result = await BOT.codex_verify_and_revise("코드를 고쳐줘", message, chat_id="chat-1")

        self.assertIn("검증 통과", result)
        codex.assert_awaited_once()
        self.assertEqual(codex.await_args.args[0], "codex")
        self.assertEqual(reviewers.await_count, 2)
        claude_kwargs = reviewers.await_args_list[0].kwargs
        self.assertTrue(claude_kwargs["fresh_session"])
        self.assertEqual(claude_kwargs["workspace_override"], str(task_workspace))

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
