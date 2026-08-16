# Telegram Meeting Interjection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: dispatch each task to a fresh subagent (this environment's Agent tool, or the Workflow tool for a deterministic multi-task pipeline — recommended) or use the executing-plans skill to work through this plan task-by-task in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human who sends a Telegram message while a `DeliberationStore` meeting is already active in that chat have the message treated as an append-only "human note" that the next agent turn picks up, instead of opening a competing independent meeting.

**Architecture:** `bin/edge_agent_deliberation.py`'s `DeliberationStore` gains an append-only `human_notes` list per session (separate from the signed `results`/`rounds` stream), a per-chat pointer file so `bin/telegram-agent-bot.py`'s `handle_message` can find "the active session for this chat" without knowing its `message_id`-derived hash, and small bookkeeping (`observed_human_seq` per recorded round, a one-shot re-integration counter) so the Claude coordinator's final-synthesis step can pick up a note that arrived after the last agent rendered evidence, exactly once. `handle_message` gains an early routing branch, before any deliberation/meeting classification, that intercepts messages for an already-open session.

**Tech Stack:** Python 3 stdlib (`fcntl` advisory locks, `tempfile` + `os.replace` atomic writes, `json`), `python-telegram-bot` (`Update`, `TelegramError`), `unittest` (`python3 -m unittest tests.<module> -v`), the repo's own `edge_agent_agent_message` signing/dedup machinery (untouched by this feature).

## Global Constraints

- Do not cancel or restart an in-flight provider call; a human note is only picked up by the *next* turn that has not started yet (spec 비목표 1).
- Do not change the `agent_message.v1` signing, barrier, or durable-dedup contract for the existing 1–3 round agent result stream; human notes live in a separate `human_notes` array, never mixed into `results`/`rounds` (spec 설계 1).
- Re-integration of late human notes into the coordinator's final synthesis is capped at **1 per session**; beyond the cap, append the note to `human_notes` (so a future meeting can pick it up) but do not re-run the final synthesis again — send a short "추가 의견은 다음 회의에서 다룹니다" notice instead (spec 설계 3, 에러 처리).
- One chat room has at most one active meeting at a time; do not build multi-session-per-chat UI (spec 비목표 3).
- This feature is Telegram-only; do not touch Discord/Mattermost adapters (spec 비목표 4).
- If `append_human_note` storage fails, tell the user in Telegram ("⚠️ 발언 반영에 실패했습니다, 다시 보내주세요") — never silently drop the note and never silently fall through to ordinary message handling (spec 에러 처리).
- A session with no `observed_human_seq` recorded (legacy/pre-feature state file) must be treated as `seq=0`, not crash (spec 에러 처리).

---

## File Structure

- **Modify `bin/edge_agent_deliberation.py`** — `DeliberationStore` gains:
  - `append_human_note(session_id, text, *, telegram_message_id)` — idempotent-by-`telegram_message_id` append to a new `human_notes` list field.
  - `latest_human_seq(session_id)` — highest recorded note `seq`, or `0`.
  - `record(...)` gains an `observed_human_seq: int = 0` keyword param, stored on the per-role result.
  - `render(...)` includes a `[사람 발언 ...]` section listing all current `human_notes`.
  - `record_active_chat_session(chat_id, session_id)` / `active_session_for_chat(chat_id)` — a per-chat pointer file (`chat-index-<sha256(chat_id)[:32]>.json`) so `telegram-agent-bot.py` can find "the active session for this chat" without recomputing a `message_id`-derived hash.
  - `close_human_notes(session_id)` — marks a session's interjection window closed (`human_notes_closed: true`) so `active_session_for_chat` stops returning it once the coordinator has sent its final answer.
  - `unreflected_human_notes(session_id)` — notes whose `seq` exceeds every recorded role's `observed_human_seq`.
  - `reintegration_count(session_id)` / `record_reintegration(session_id)` — the 1-per-session re-synthesis cap counter.
- **Modify `bin/edge_agent_ingress.py`** — add `is_execution_directive(text) -> bool`, a thin public wrapper around the existing private `_EXECUTION_ACTION` regex (currently only reachable indirectly through `is_conversation_meeting`), so `telegram-agent-bot.py` can ask "is this clearly an execution instruction?" independent of whether it's also a deliberation request.
- **Modify `bin/telegram-agent-bot.py`**:
  - `handle_message` — new early-routing block (after the existing ingress-claim/cancel/search checks, before the `_BUSY_LOCK` guard) that detects an active meeting for the chat and, unless the text is an explicit execution directive, appends a human note and returns instead of falling through to ordinary/meeting handling.
  - The existing `if is_deliberation_request(text) and classify_ingress(text).accepts("claude"):` block (inside `_BUSY_LOCK`) additionally calls `record_active_chat_session` right after `DeliberationStore().start(...)`.
  - The Claude-coordinator final-synthesis branch (the `else:` after the round-3 `_require_deliberation_round` call) gains the capped re-integration check before/after building `final_evidence`, and calls `close_human_notes` once the final reply is ready.
- **Test: `tests/test_edge_agent_deliberation.py`** — new tests for `append_human_note`, `observed_human_seq`, `render()`'s note section, the chat-session pointer trio, `unreflected_human_notes`, and the reintegration counter.
- **Test: `tests/test_edge_agent_ingress.py`** — new test for `is_execution_directive`.
- **Test: `tests/test_telegram_execution_contract.py`** — two new `handle_message`-level tests (interjection routing, and the no-active-session regression case), following the existing `test_legacy_meeting_coordination_forwards_read_only_state` pattern in that file.

---

### Task 1: `DeliberationStore.append_human_note` and the `human_notes` schema field

**Files:**
- Modify: `bin/edge_agent_deliberation.py:263` (just above `def record`, add the new method after `record` ends, i.e. after line 371/before `def snapshot` at line 373)
- Test: `tests/test_edge_agent_deliberation.py`

**Interfaces:**
- Produces: `DeliberationStore.append_human_note(self, session_id: str, text: str, *, telegram_message_id: object) -> dict[str, Any]` — returns the updated session payload. Idempotent: calling twice with the same `telegram_message_id` returns the payload unchanged (no duplicate note). Raises `ValueError("unknown deliberation session")` if `session_id` has no existing session (mirrors the `_safe_session` guard style already used by `record`/`start`).
- Consumes: nothing new; uses existing `self._lock()`, `self._read()`, `self._write()`, `_safe_session()`, `_bounded()` already defined in this file.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_edge_agent_deliberation.py`, inside `class DeliberationStoreTests`:

```python
    def test_append_human_note_is_ordered_and_idempotent_by_message_id(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 200)
                store.start(session_id, "회의 시작")
                store.append_human_note(session_id, "첫 발언", telegram_message_id=201)
                store.append_human_note(session_id, "둘째 발언", telegram_message_id=202)
                notes = store.snapshot(session_id)["human_notes"]
                self.assertEqual([note["seq"] for note in notes], [1, 2])
                self.assertEqual(notes[0]["text"], "첫 발언")
                self.assertEqual(notes[0]["telegram_message_id"], "201")
                # Re-appending the same telegram_message_id must not duplicate.
                store.append_human_note(session_id, "첫 발언 재전송", telegram_message_id=201)
                self.assertEqual(len(store.snapshot(session_id)["human_notes"]), 2)

    def test_append_human_note_rejects_unknown_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliberationStore(directory)
            with self.assertRaises(ValueError):
                store.append_human_note("delib-missing", "발언", telegram_message_id=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_edge_agent_deliberation -v`
Expected: FAIL with `AttributeError: 'DeliberationStore' object has no attribute 'append_human_note'`

- [ ] **Step 3: Write minimal implementation**

Insert into `bin/edge_agent_deliberation.py` immediately after the `record()` method ends (after line 371, before `def snapshot` at line 373):

```python
    def append_human_note(self, session_id: str, text: str, *, telegram_message_id: object) -> dict[str, Any]:
        """Record a human interjection as append-only evidence, never mixed
        into the signed agent `results`/`rounds` stream (see design doc
        docs/specs/2026-08-16-telegram-meeting-interjection-design.md 설계 1).
        """
        session_id = _safe_session(session_id)
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(session_id)
            if payload is None:
                raise ValueError("unknown deliberation session")
            notes = list(payload.get("human_notes") or [])
            message_id_text = str(telegram_message_id)
            for note in notes:
                if str(note.get("telegram_message_id")) == message_id_text:
                    return payload
            next_seq = max((int(note.get("seq", 0)) for note in notes), default=0) + 1
            notes.append({
                "seq": next_seq,
                "text": _bounded(text),
                "telegram_message_id": message_id_text,
                "recorded_at": time.time(),
            })
            payload["human_notes"] = notes
            self._write(session_id, payload)
            return payload
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_edge_agent_deliberation -v`
Expected: PASS (all tests, including the two new ones)

- [ ] **Step 5: Commit**

```bash
cd /Users/edge_ai/mac-agent
git add bin/edge_agent_deliberation.py tests/test_edge_agent_deliberation.py
git commit -m "feat: add DeliberationStore.append_human_note for meeting interjections"
```

---

### Task 2: `observed_human_seq` on recorded rounds, `latest_human_seq`, and `render()` note section

**Files:**
- Modify: `bin/edge_agent_deliberation.py:263-299` (`record` signature and `result` dict), `bin/edge_agent_deliberation.py:423-466` (`render`)
- Test: `tests/test_edge_agent_deliberation.py`

**Interfaces:**
- Consumes: `payload.get("human_notes")` written by Task 1's `append_human_note`.
- Produces: `DeliberationStore.record(..., observed_human_seq: int = 0)` — every recorded result dict gains an `"observed_human_seq"` int field (default `0`). `DeliberationStore.latest_human_seq(self, session_id: str) -> int` — highest `seq` among `human_notes`, or `0` if none/missing (this is the hard-coded fallback the design doc's 에러 처리 section requires for legacy state). `render()`'s returned string additionally contains one line per human note, each starting with `- seq=`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_edge_agent_deliberation.py`:

```python
    def test_record_stores_observed_human_seq_and_defaults_to_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 210)
                store.start(session_id, "회의")
                store.append_human_note(session_id, "발언1", telegram_message_id=1)
                self.assertEqual(store.latest_human_seq(session_id), 1)
                store.record(session_id, "roda", status="completed", summary="roda 의견", observed_human_seq=store.latest_human_seq(session_id))
                self.assertEqual(store.snapshot(session_id)["results"]["roda"]["observed_human_seq"], 1)
                store.record(session_id, "codex", status="completed", summary="codex 의견")
                self.assertEqual(store.snapshot(session_id)["results"]["codex"]["observed_human_seq"], 0)
                self.assertEqual(store.latest_human_seq("delib-does-not-exist"), 0)

    def test_render_includes_unreflected_human_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 211)
                store.start(session_id, "회의")
                store.append_human_note(session_id, "중간에 끼어든 발언", telegram_message_id=5)
                rendered = store.render(session_id)
                self.assertIn("seq=1", rendered)
                self.assertIn("중간에 끼어든 발언", rendered)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_edge_agent_deliberation -v`
Expected: FAIL — `record()` raises `TypeError: record() got an unexpected keyword argument 'observed_human_seq'`, and the render test fails with `AttributeError: 'DeliberationStore' object has no attribute 'latest_human_seq'`.

- [ ] **Step 3: Write minimal implementation**

In `bin/edge_agent_deliberation.py`, change the `record` signature (line 263):

```python
    def record(self, session_id: str, role: str, *, status: str, summary: str, evidence_refs: tuple[str, ...] = (), round_number: int | None = None, observed_human_seq: int = 0) -> dict[str, Any]:
```

In the `result` dict construction (lines 293-299), add the new field:

```python
            result = {
                "status": status,
                "summary": _bounded(summary),
                "evidence_refs": list(effective_evidence_refs),
                "recorded_epoch": time.time(),
                "round": current_round,
                "observed_human_seq": max(0, int(observed_human_seq)),
            }
```

Add a new method right after `append_human_note` (from Task 1):

```python
    def latest_human_seq(self, session_id: str) -> int:
        payload = self.snapshot(session_id) or {}
        notes = payload.get("human_notes") or []
        return max((int(note.get("seq", 0)) for note in notes), default=0)
```

In `render()` (lines 423-466), insert a human-notes section before the `bus_delivery=` line. The method currently ends:

```python
        lines.append(f"bus_delivery={bus_ack_status}")
        lines.append(f"barrier_status={payload.get('status', 'not_observed')}; round={payload.get('round', 1)}")
        return "\n".join(lines)[:7200]
```

Change to:

```python
        notes = payload.get("human_notes") or []
        if notes:
            lines.append("[사람 발언 — 아직 반영되지 않았을 수 있음]")
            for note in notes:
                lines.append(f"- seq={note.get('seq')}: {_bounded(note.get('text', ''))}")
        lines.append(f"bus_delivery={bus_ack_status}")
        lines.append(f"barrier_status={payload.get('status', 'not_observed')}; round={payload.get('round', 1)}")
        return "\n".join(lines)[:7200]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_edge_agent_deliberation -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/edge_ai/mac-agent
git add bin/edge_agent_deliberation.py tests/test_edge_agent_deliberation.py
git commit -m "feat: bind observed_human_seq to each recorded round and render human notes"
```

---

### Task 3: chat-session pointer (`record_active_chat_session` / `active_session_for_chat` / `close_human_notes`)

**Files:**
- Modify: `bin/edge_agent_deliberation.py` (new methods, added after `latest_human_seq` from Task 2)
- Test: `tests/test_edge_agent_deliberation.py`

**Interfaces:**
- Consumes: `self._lock()`, `self._read()`, `self._write()`, `_safe_session()` (existing); `hashlib` (already imported at the top of the module, line 7).
- Produces:
  - `DeliberationStore.record_active_chat_session(self, chat_id: object, session_id: str) -> None`
  - `DeliberationStore.active_session_for_chat(self, chat_id: object) -> str | None` — returns the pointed-to `session_id` only if the session exists, is not `human_notes_closed`, and its `status` is not in `{"barrier_ready", "failed"}`; otherwise `None`.
  - `DeliberationStore.close_human_notes(self, session_id: str) -> dict[str, Any]` — sets `human_notes_closed: True` on the session payload; no-op (`{}`) if the session doesn't exist.

Task 4 and Task 5 call all three of these by exactly these names.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_edge_agent_deliberation.py`:

```python
    def test_active_session_for_chat_tracks_pointer_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                chat_id = -1003952617795
                session_id = session_id_for_telegram(chat_id, 220)
                self.assertIsNone(store.active_session_for_chat(chat_id))
                store.start(session_id, "회의")
                store.record_active_chat_session(chat_id, session_id)
                self.assertEqual(store.active_session_for_chat(chat_id), session_id)
                store.close_human_notes(session_id)
                self.assertIsNone(store.active_session_for_chat(chat_id))

    def test_active_session_for_chat_is_none_once_barrier_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                chat_id = -1003952617795
                session_id = session_id_for_telegram(chat_id, 221)
                store.start(session_id, "회의", mode="conversation")
                store.record_active_chat_session(chat_id, session_id)
                self.assertEqual(store.active_session_for_chat(chat_id), session_id)
                for role in ("claude", "codex", "antigravity", "roda"):
                    store.record(session_id, role, status="completed", summary=f"{role} 의견")
                self.assertEqual(store.snapshot(session_id)["status"], "barrier_ready")
                self.assertIsNone(store.active_session_for_chat(chat_id))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_edge_agent_deliberation -v`
Expected: FAIL with `AttributeError: 'DeliberationStore' object has no attribute 'active_session_for_chat'`

- [ ] **Step 3: Write minimal implementation**

Add to `bin/edge_agent_deliberation.py`, after `latest_human_seq`:

```python
    def _chat_index_id(self, chat_id: object) -> str:
        digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:32]
        return f"chat-index-{digest}"

    def record_active_chat_session(self, chat_id: object, session_id: str) -> None:
        session_id = _safe_session(session_id)
        index_id = self._chat_index_id(chat_id)
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._write(index_id, {
                "schema": "edge_agent.deliberation_chat_index.v1",
                "session_id": session_id,
                "updated_epoch": time.time(),
            })
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def active_session_for_chat(self, chat_id: object) -> str | None:
        index_id = self._chat_index_id(chat_id)
        pointer = self._read(index_id)
        if not pointer:
            return None
        session_id = str(pointer.get("session_id") or "")
        if not session_id:
            return None
        payload = self._read(session_id)
        if payload is None:
            return None
        if payload.get("human_notes_closed") is True:
            return None
        if payload.get("status") in {"barrier_ready", "failed"}:
            return None
        return session_id

    def close_human_notes(self, session_id: str) -> dict[str, Any]:
        session_id = _safe_session(session_id)
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(session_id)
            if payload is None:
                return {}
            payload["human_notes_closed"] = True
            self._write(session_id, payload)
            return payload
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
```

Note: `_chat_index_id` deliberately reuses `self._write`/`self._read`, which key off `self._path(session_id)` = `self.root / f"{_safe_session(session_id)}.json"`. Real session ids always start with `delib-` (see `session_id_for_telegram`), so a `chat-index-<hash>.json` filename can never collide with a session file.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_edge_agent_deliberation -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/edge_ai/mac-agent
git add bin/edge_agent_deliberation.py tests/test_edge_agent_deliberation.py
git commit -m "feat: add per-chat active-session pointer to DeliberationStore"
```

---

### Task 4: `is_execution_directive` + `handle_message` interjection routing

**Files:**
- Modify: `bin/edge_agent_ingress.py` (new function, after `is_conversation_meeting`, currently ending at line 281)
- Modify: `bin/telegram-agent-bot.py:67-82` (imports), `bin/telegram-agent-bot.py:2367-2377` (new early-routing block), `bin/telegram-agent-bot.py:2389-2396` (wire `record_active_chat_session`)
- Test: `tests/test_edge_agent_ingress.py`, `tests/test_telegram_execution_contract.py`

**Interfaces:**
- Consumes: `DeliberationStore.active_session_for_chat`, `.append_human_note`, `.record_active_chat_session` (Tasks 1–3); `CONVERSATION_COORDINATOR_ROLE` from `edge_agent_deliberation` (already defined at module level, line 23 — not previously imported into `telegram-agent-bot.py`).
- Produces: `edge_agent_ingress.is_execution_directive(text: str) -> bool`. No new names produced from `telegram-agent-bot.py` — this task only wires existing/Task-1-3 names into `handle_message`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_edge_agent_ingress.py`, inside `class IngressRoutingTests`:

```python
    def test_is_execution_directive_matches_explicit_action_verbs(self):
        self.assertTrue(is_execution_directive("이 코드를 구현해줘"))
        self.assertTrue(is_execution_directive("파일을 확인해줘"))
        self.assertFalse(is_execution_directive("이 부분 어떻게 생각해?"))
        self.assertFalse(is_execution_directive("장단점을 회의해줘"))
```

Update the import block at the top of the file:

```python
from edge_agent_ingress import (  # noqa: E402
    classify,
    is_conversation_meeting,
    is_deliberation_request,
    is_execution_directive,
    is_group_address,
    routing_projection,
)
```

Add to `tests/test_telegram_execution_contract.py`, inside `class TelegramExecutionContractTests`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_edge_agent_ingress -v`
Expected: FAIL with `ImportError: cannot import name 'is_execution_directive'`

Run: `python3 -m unittest tests.test_telegram_execution_contract -v`
Expected: FAIL on `test_active_meeting_interjection_appends_note_without_new_session` — `provider.assert_not_awaited()` fails because nothing currently intercepts the message before it falls through to ordinary handling (the store has no `human_notes` key yet since nothing appended one).

- [ ] **Step 3: Write minimal implementation**

In `bin/edge_agent_ingress.py`, add after `is_conversation_meeting` (after line 281):

```python
def is_execution_directive(text: str) -> bool:
    """Return whether the current directive is an explicit execution verb.

    Used to keep an in-progress meeting from swallowing a clear "do this
    now" instruction as a mere discussion note (see design doc 설계 4).
    """
    return bool(_EXECUTION_ACTION.search(routing_projection(text)))
```

In `bin/telegram-agent-bot.py`, update the `edge_agent_deliberation` import block (lines 67-76):

```python
from edge_agent_deliberation import (
    CONVERSATION_COORDINATOR_ROLE,
    DeliberationStore,
    configured_barrier_timeout_seconds,
    configured_conversation_timeout_seconds,
    first_pass_prompt,
    roles_for_request,
    session_id_for_telegram,
    should_publish_failure,
    should_publish_user_result,
)
```

and the `edge_agent_ingress` import block (lines 77-82):

```python
from edge_agent_ingress import (
    classify as classify_ingress,
    is_conversation_meeting,
    is_deliberation_request,
    is_execution_directive,
    is_group_address,
)
```

Insert a new block into `handle_message` right after the `is_online_search_request` early-return (currently lines 2367-2375, ending with `return`) and before `if _BUSY_LOCK.locked():` (line 2377):

```python
    active_meeting_session_id = None
    if chat is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            active_meeting_session_id = DeliberationStore().active_session_for_chat(message.chat_id)
        except (OSError, ValueError, TypeError) as exc:
            log(f"활성 회의 세션 조회 실패(새 요청으로 계속 진행): {type(exc).__name__}")
            active_meeting_session_id = None
    if active_meeting_session_id and not is_execution_directive(text):
        try:
            DeliberationStore().append_human_note(
                active_meeting_session_id,
                text,
                telegram_message_id=message.message_id,
            )
        except (OSError, ValueError, TypeError) as exc:
            log(f"회의 발언 반영 실패 session={active_meeting_session_id} error={type(exc).__name__}")
            if ROLE == CONVERSATION_COORDINATOR_ROLE:
                try:
                    await message.reply_text("⚠️ 발언 반영에 실패했습니다, 다시 보내주세요")
                except TelegramError as send_exc:
                    log(f"발언 반영 실패 안내 전송도 실패: {send_exc}")
            _complete_ingress_claim()
            return
        if ROLE == CONVERSATION_COORDINATOR_ROLE:
            try:
                await message.reply_text("💬 다음 회의 발언에 반영됩니다")
            except TelegramError as exc:
                log(f"회의 발언 확인 응답 전송 실패: {exc}")
        _complete_ingress_claim()
        return
```

This block runs after `_complete_ingress_claim` is already defined (line 2300) and after `chat`/`message`/`text` are already bound (lines 2245-2248), so it has everything it needs in scope. Only `CONVERSATION_COORDINATOR_ROLE` (`"codex"`) sends the Telegram ack/error — matching the existing `should_publish_failure` convention of "only the deputy/coordinator role talks for the group" — so the other 3 per-role bot processes append the (idempotent) note silently and do not spam 4 Telegram replies for one human message.

Then, in the existing deliberation-start block inside `_BUSY_LOCK` (currently lines 2389-2396):

```python
        if is_deliberation_request(text) and classify_ingress(text).accepts("claude"):
            deliberation_session_id = session_id_for_telegram(message.chat_id, message.message_id)
            DeliberationStore().start(
                deliberation_session_id,
                text,
                roles=roles_for_request(text),
                mode="conversation" if conversation_meeting_active else "verified",
            )
            DeliberationStore().record_active_chat_session(message.chat_id, deliberation_session_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_edge_agent_ingress -v`
Expected: PASS

Run: `python3 -m unittest tests.test_telegram_execution_contract -v`
Expected: PASS (all tests, including both new ones)

- [ ] **Step 5: Commit**

```bash
cd /Users/edge_ai/mac-agent
git add bin/edge_agent_ingress.py bin/telegram-agent-bot.py tests/test_edge_agent_ingress.py tests/test_telegram_execution_contract.py
git commit -m "feat: route interjections during an active meeting to append_human_note"
```

---

### Task 5: capped coordinator re-integration before final synthesis

**Files:**
- Modify: `bin/edge_agent_deliberation.py` (new methods, added after `close_human_notes` from Task 3)
- Modify: `bin/telegram-agent-bot.py:2687-2710` (Claude-coordinator final-synthesis branch)
- Test: `tests/test_edge_agent_deliberation.py`, `tests/test_telegram_execution_contract.py`

**Interfaces:**
- Consumes: `DeliberationStore.render`, `.append_human_note`, `.close_human_notes` (Tasks 1–3); `_require_deliberation_round`, `run_provider`, `_notify_waiting` (existing, unchanged).
- Produces: `DeliberationStore.unreflected_human_notes(self, session_id: str) -> tuple[dict[str, Any], ...]`, `DeliberationStore.reintegration_count(self, session_id: str) -> int`, `DeliberationStore.record_reintegration(self, session_id: str) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_edge_agent_deliberation.py`:

```python
    def test_unreflected_human_notes_and_reintegration_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            with patch.dict("os.environ", {"EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path)}):
                store = DeliberationStore(directory)
                session_id = session_id_for_telegram("-1", 230)
                store.start(session_id, "회의")
                store.record(session_id, "roda", status="completed", summary="roda 의견", observed_human_seq=0)
                self.assertEqual(store.unreflected_human_notes(session_id), ())
                store.append_human_note(session_id, "뒤늦은 발언", telegram_message_id=1)
                unreflected = store.unreflected_human_notes(session_id)
                self.assertEqual(len(unreflected), 1)
                self.assertEqual(unreflected[0]["text"], "뒤늦은 발언")
                self.assertEqual(store.reintegration_count(session_id), 0)
                store.record_reintegration(session_id)
                self.assertEqual(store.reintegration_count(session_id), 1)
```

Add to `tests/test_telegram_execution_contract.py`, inside `class TelegramExecutionContractTests`:

```python
    async def test_coordinator_reintegrates_late_note_once_before_final_synthesis(self):
        outputs = iter(["claude 1차", "claude 2차", "claude 3차", "재종합된 최종 답변"])

        async def provider(*args, **kwargs):
            return next(outputs)

        sent = FakeSent(14)
        update = make_update(sent)
        chat_id = update.effective_chat.id
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as key_dir:
            key_path = Path(key_dir) / "agent-message.key"
            key_path.write_bytes(b"local-test-key-with-more-than-16-bytes")
            key_path.chmod(0o600)
            store = BOT.DeliberationStore(directory)
            with patch.dict(os.environ, {
                "EDGE_AGENT_MESSAGE_KEY_FILE": str(key_path),
                "EDGE_AGENT_TELEGRAM_DELIVERY_ROOT": self.delivery_root.name,
            }), \
                    patch.object(BOT, "addressed_text", return_value="논의하고 통합해줘"), \
                    patch.object(BOT, "DeliberationStore", return_value=store), \
                    patch.object(BOT, "SIMPLE_MEETING_MODE", False), \
                    patch.object(BOT, "is_deliberation_request", return_value=True), \
                    patch.object(BOT, "_prepare_context", return_value=None), \
                    patch.object(BOT, "_ingress_identity", return_value=None), \
                    patch.object(BOT, "_is_stale", return_value=False), \
                    patch.object(BOT, "_needs_task_worktree", return_value=False), \
                    patch.object(BOT, "run_provider", new=provider), \
                    patch.object(BOT, "write_task_state", return_value="task-final"), \
                    patch.object(BOT, "start_session", return_value="session-final"), \
                    patch.object(BOT, "update_session"), \
                    patch.object(BOT, "write_reflection"), \
                    patch.object(BOT, "_record_telegram_efficiency"), \
                    patch.object(BOT, "_update_task_worktree_status"), \
                    patch.object(BOT, "ROLE", "claude"):
                BOT.ACTIVE_TASK_WORKSPACE = None
                BOT.ACTIVE_LOGICAL_SESSION_ID = None

                async def fake_require_round(session_id, round_number, *, timeout_seconds=None):
                    if round_number == 3:
                        for role in ("codex", "antigravity", "roda"):
                            store.record(session_id, role, status="completed", summary=f"{role} 3차", round_number=3)
                        store.append_human_note(session_id, "3차 도중 도착한 발언", telegram_message_id=555)

                with patch.object(BOT, "_require_deliberation_round", side_effect=lambda *a, **k: None):
                    with patch.object(BOT.asyncio, "to_thread", new=AsyncMock(side_effect=fake_require_round)):
                        await BOT.handle_message(update, SimpleNamespace())

        session_id = BOT.session_id_for_telegram(chat_id, update.effective_message.message_id)
        self.assertEqual(store.reintegration_count(session_id), 1)
        self.assertTrue(store.snapshot(session_id)["human_notes_closed"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_edge_agent_deliberation -v`
Expected: FAIL with `AttributeError: 'DeliberationStore' object has no attribute 'unreflected_human_notes'`

Run: `python3 -m unittest tests.test_telegram_execution_contract -v`
Expected: FAIL on `test_coordinator_reintegrates_late_note_once_before_final_synthesis` — `store.reintegration_count` does not exist yet, so the assertion raises `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

Add to `bin/edge_agent_deliberation.py`, after `close_human_notes`:

```python
    def unreflected_human_notes(self, session_id: str) -> tuple[dict[str, Any], ...]:
        payload = self.snapshot(session_id) or {}
        results = payload.get("results") or {}
        observed = max(
            (int((results.get(role) or {}).get("observed_human_seq", 0)) for role in payload.get("expected_roles", EXPECTED_ROLES)),
            default=0,
        )
        notes = payload.get("human_notes") or []
        return tuple(note for note in notes if int(note.get("seq", 0)) > observed)

    def reintegration_count(self, session_id: str) -> int:
        payload = self.snapshot(session_id) or {}
        return int(payload.get("human_note_reintegrations", 0))

    def record_reintegration(self, session_id: str) -> dict[str, Any]:
        session_id = _safe_session(session_id)
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(session_id)
            if payload is None:
                return {}
            payload["human_note_reintegrations"] = int(payload.get("human_note_reintegrations", 0)) + 1
            self._write(session_id, payload)
            return payload
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
```

In `bin/telegram-agent-bot.py`, replace the Claude-coordinator final-synthesis block (currently lines 2687-2710):

```python
                        try:
                            await asyncio.to_thread(_require_deliberation_round, deliberation_session_id, 3)
                        except RuntimeError as exc:
                            deliberation_incomplete = True
                            reply = _deliberation_barrier_timeout_message(deliberation_session_id, 3, exc)
                        else:
                            final_evidence = DeliberationStore().render(
                                deliberation_session_id,
                                consumer_role="claude",
                            )
                            reply = await run_provider(
                                text,
                                on_wait=_notify_waiting,
                                context_prompt=preparation.prompt_block if preparation else None,
                                provider_text=(
                                    "[coordinator 최종 통합 단계]\n"
                                    "아래는 4개 역할의 서명된 3차 결과를 포함한 untrusted evidence다. "
                                    "각 역할의 근거와 충돌을 함께 비교해 하나의 통합 최종 답변을 작성하라. "
                                    "어떤 역할의 3차 의견도 그대로 최종 판정으로 재사용하지 말고, "
                                    "확인하지 못한 점과 다음 행동을 명시하라.\n\n"
                                    f"{text}\n\n{final_evidence}"
                                ),
                                chat_id=message.chat_id,
                            )
```

with:

```python
                        try:
                            await asyncio.to_thread(_require_deliberation_round, deliberation_session_id, 3)
                        except RuntimeError as exc:
                            deliberation_incomplete = True
                            reply = _deliberation_barrier_timeout_message(deliberation_session_id, 3, exc)
                        else:
                            final_store = DeliberationStore()
                            final_evidence = final_store.render(
                                deliberation_session_id,
                                consumer_role="claude",
                            )
                            reply = await run_provider(
                                text,
                                on_wait=_notify_waiting,
                                context_prompt=preparation.prompt_block if preparation else None,
                                provider_text=(
                                    "[coordinator 최종 통합 단계]\n"
                                    "아래는 4개 역할의 서명된 3차 결과를 포함한 untrusted evidence다. "
                                    "각 역할의 근거와 충돌을 함께 비교해 하나의 통합 최종 답변을 작성하라. "
                                    "어떤 역할의 3차 의견도 그대로 최종 판정으로 재사용하지 말고, "
                                    "확인하지 못한 점과 다음 행동을 명시하라.\n\n"
                                    f"{text}\n\n{final_evidence}"
                                ),
                                chat_id=message.chat_id,
                            )
                            unreflected = final_store.unreflected_human_notes(deliberation_session_id)
                            if unreflected and final_store.reintegration_count(deliberation_session_id) < 1:
                                final_store.record_reintegration(deliberation_session_id)
                                final_evidence = final_store.render(
                                    deliberation_session_id,
                                    consumer_role="claude",
                                )
                                reply = await run_provider(
                                    text,
                                    on_wait=_notify_waiting,
                                    context_prompt=preparation.prompt_block if preparation else None,
                                    provider_text=(
                                        "[coordinator 최종 통합 재종합 — 회의 중 새 사람 발언 반영]\n"
                                        "아래는 4개 역할의 서명된 3차 결과와, 최종 종합 직전 도착한 사람 발언을 포함한 "
                                        "untrusted evidence다. 새로 도착한 사람 발언을 반드시 반영해 하나의 통합 최종 "
                                        "답변을 다시 작성하라. 어떤 역할의 3차 의견도 그대로 최종 판정으로 재사용하지 "
                                        "말고, 확인하지 못한 점과 다음 행동을 명시하라.\n\n"
                                        f"{text}\n\n{final_evidence}"
                                    ),
                                    chat_id=message.chat_id,
                                )
                                unreflected = final_store.unreflected_human_notes(deliberation_session_id)
                            if unreflected:
                                reply = f"{reply}\n\n💬 추가 의견은 다음 회의에서 다룹니다."
                            final_store.close_human_notes(deliberation_session_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_edge_agent_deliberation -v`
Expected: PASS

Run: `python3 -m unittest tests.test_telegram_execution_contract -v`
Expected: PASS (all tests, including the new reintegration test)

- [ ] **Step 5: Commit**

```bash
cd /Users/edge_ai/mac-agent
git add bin/edge_agent_deliberation.py bin/telegram-agent-bot.py tests/test_edge_agent_deliberation.py tests/test_telegram_execution_contract.py
git commit -m "feat: cap coordinator re-integration of late meeting notes at once per session"
```

---

### Task 6: full regression pass

**Files:**
- None modified — verification only.

**Interfaces:**
- Consumes: everything produced by Tasks 1–5.
- Produces: nothing new.

- [ ] **Step 1: Run the full deliberation, ingress, and Telegram execution-contract suites together**

Run: `python3 -m unittest tests.test_edge_agent_deliberation tests.test_edge_agent_ingress tests.test_telegram_execution_contract tests.test_context_envelope_continuity -v`
Expected: PASS — no test from any of the four modules regresses. (`test_context_envelope_continuity.py` is included because it loads the same `telegram-agent-bot.py` module via `importlib` and exercises `handle_message`-adjacent code paths; it must still pass unchanged since this feature only added a new early-return branch and did not change any existing branch's behavior when no active session exists.)

- [ ] **Step 2: Run the full repo test suite once, to catch anything outside the four targeted modules**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v 2>&1 | tail -40`
Expected: no new failures compared to a pre-change baseline (some pre-existing skips/failures unrelated to this feature may already exist in the repo; only confirm nothing *new* broke).

- [ ] **Step 3: Commit (only if Step 1/2 required any fixup)**

If Steps 1–2 pass cleanly with no code changes, there is nothing to commit for this task — it is a verification checkpoint, not a code change. If a fixup was needed, commit it separately with a message describing exactly what regressed and why.

---

## Self-Review

**1. Spec coverage.**
- 설계 1 (append-only human note stream, separate from `results`/`rounds`) → Task 1.
- 설계 2 (`observed_human_seq` binding at each round's `record()`, notes visible in `render()`) → Task 2.
- 설계 3 (pre-final-synthesis re-check, capped at 1 re-integration, "다음 회의에서 다룹니다" notice beyond the cap) → Task 5.
- 설계 4 (active-session detection before `is_conversation_meeting` classification, execution-directive bypass) → Task 4 (`active_session_for_chat` built in Task 3; `is_execution_directive` and the `handle_message` wiring in Task 4).
- 에러 처리 (`append_human_note` failure → `"⚠️ 발언 반영에 실패했습니다, 다시 보내주세요"`, not silently swallowed; cap-exhausted repeats send the notice every time rather than going silent; `observed_human_seq` missing → treated as `seq=0`) → Task 4's failure branch, Task 5's `unreflected` check re-running every coordinator pass, and Task 2/5's `max(..., default=0)` fallbacks, respectively.
- 테스트 section's four required cases → explicitly present: interjection-not-new-session (Task 4), no-active-session regression (Task 4), capped reintegration + repeat notice (Task 5), `observed_human_seq` on recorded rounds (Task 2).
- 비목표 1 (no cancelling in-flight calls) → satisfied structurally: `append_human_note` never touches a running `run_provider` call; it only affects the *next* `render()`/`record()` pair.
- 비목표 2 (`agent_message.v1` signing/barrier/dedup unchanged) → verified by reading `record()`'s signing block (lines 300-326 of the pre-change file); Task 2 only adds a plain `observed_human_seq` int field to the *unsigned* `result` dict entries that surround the signed `agent_message` sub-object — it does not touch `build_message`/`verify_message`/`_dedup.accept`.
- 비목표 3 (one active session per chat) → `record_active_chat_session`/`active_session_for_chat` (Task 3) is a single pointer per chat, overwritten on each new `start()`, matching the existing one-session-per-chat assumption already baked into `session_id_for_telegram`.

**2. Placeholder scan.** No "TBD"/"similar to Task N" text; every step shows the literal code to write. The one deliberately-loose piece — "some pre-existing skips/failures unrelated to this feature may already exist" in Task 6 Step 2 — is a verification instruction, not an implementation placeholder, and is scoped to "don't regress," which is checkable.

**3. Type/signature consistency.** Verified across tasks: `append_human_note(session_id, text, *, telegram_message_id)` (Task 1) is called identically in Task 4 and Task 5's test. `record(..., observed_human_seq=0)` (Task 2) is the exact keyword used in Task 5's coordinator wiring (implicitly defaulted, since the coordinator path in this codebase does not currently thread a per-role `observed_human_seq` into the 3-round `record()` calls it already makes — see flag below). `active_session_for_chat`, `record_active_chat_session`, `close_human_notes` (Task 3) are called with matching names and argument order in Task 4 and Task 5. `unreflected_human_notes`, `reintegration_count`, `record_reintegration` (Task 5) are self-contained to that task.

**Flag for the caller — one real spec/code gap found, and one scope decision made beyond the spec's literal text:**

1. **`observed_human_seq` is only *wired into* the Task 5 final-synthesis re-check's `unreflected_human_notes` computation via the round-3 `record()` calls that already exist in `handle_message` (both the Claude-coordinator branch and the non-coordinator/peer branch) — but this plan does not add `observed_human_seq=...render()`-derived values to *every* existing `record()` call site in `telegram-agent-bot.py` (there are roughly a dozen, across 1st/2nd/3rd round and both coordinator and peer code paths, e.g. lines 2607, 2640, 2658, 2680, 2719, 2743, 2773).** Doing so exhaustively was out of this plan's scope because the spec's 테스트 section only asks to verify "observed_human_seq가 각 라운드 기록에 정상적으로 남는지" (Task 2's test covers the mechanism), not that every call site thread it through. Left as-is, `unreflected_human_notes` in Task 5 will only ever see the coordinator's own `observed_human_seq` (defaulted to `0` unless separately wired), which under-counts what other agents have "seen." A tight follow-up (not included here, since it touches ~10 call sites purely to pass a value through, with no new test-observable behavior beyond what Task 2/5 already cover) would thread `store.render(...)` + `store.latest_human_seq(session_id)` together at each of those call sites and pass the result into the paired `record(..., observed_human_seq=...)` call.
2. **Scope decision beyond the spec's literal words:** the spec's 설계 3 names the re-check point as "3차 barrier 통과 직전," which only exists in the `mode="verified"`/`ROLE == "claude"` coordinator branch (3 rounds). This plan does **not** add an equivalent re-check to the `conversation_meeting_active`/`ROLE == "codex"` single-round branch (lines 2606-2629 of the pre-change file) — that branch's "final synthesis" is its *only* round, so there is no round-3-vs-earlier-rounds distinction to re-check against, and the spec doesn't describe a re-check for that path. If interjections during a `conversation` meeting matter as much as during a `verified` meeting, that would need its own design note before implementation, since the single-round codex-coordinator path has no natural "pre-final-synthesis, post-barrier" seam to hook into the way the 3-round claude-coordinator path does.
