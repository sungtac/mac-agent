#!/usr/bin/env python3
"""Atomic logical-session context and handoff event store.

The store keeps a compact session snapshot plus an append-only event journal.
It is a local persistence primitive, not a router or provider executor.  Raw
transcripts and sensitive material are rejected; adapters must provide bounded
summaries and evidence references instead.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from edge_agent_session_contract import LogicalSession, Provider, SessionChannel, SessionStatus, load_logical_session
from edge_agent_secure_paths import append_text, ensure_private_directory, open_lock, read_text


EVENT_SCHEMA = "edge_agent.context_event.v1"
DEFAULT_MAX_CONTEXT_CHARS = 6000
_SENSITIVE_MARKERS = (
    "token=",
    "api_key",
    "api key",
    "authorization:",
    "bearer ",
    "password=",
    "cookie:",
    "secret=",
    "private key",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_component(value: str) -> str:
    value = str(value or "").strip()
    if not value or any(char in value for char in "/\\\x00"):
        raise ValueError("invalid store path component")
    return value


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in _SENSITIVE_MARKERS)
    if isinstance(value, Mapping):
        return any(_contains_sensitive(key) or _contains_sensitive(item) for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_sensitive(item) for item in value)
    return False


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class ContextStore:
    """Persist session snapshots and bounded handoff events under one root."""

    def __init__(self, root: str | Path | None = None, *, max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS):
        self.root = Path(root or Path.home() / ".edge-agent" / "sessions").expanduser()
        self.max_context_chars = max(1000, int(max_context_chars))

    def _paths(self, session_id: str) -> tuple[Path, Path, Path]:
        ensure_private_directory(self.root)
        safe = _safe_component(session_id)
        snapshot_root = ensure_private_directory(self.root / "snapshots")
        event_root = ensure_private_directory(self.root / "events")
        lock_root = ensure_private_directory(self.root / "locks")
        return (
            snapshot_root / f"{safe}.json",
            event_root / f"{safe}.jsonl",
            lock_root / f"{safe}.lock",
        )

    @contextlib.contextmanager
    def _locked(self, session_id: str) -> Iterator[None]:
        _, _, lock_path = self._paths(session_id)
        descriptor = open_lock(lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _save_unlocked(self, session: LogicalSession) -> None:
        snapshot_path, _, _ = self._paths(session.logical_session_id)
        _atomic_write_json(snapshot_path, session.to_dict())

    def _append_event_unlocked(self, session: LogicalSession, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not event_type.strip() or any(char.isspace() for char in event_type):
            raise ValueError("event_type must be a compact non-empty value")
        if _contains_sensitive(payload):
            raise ValueError("context event contains a sensitive marker")
        event = {
            "schema": EVENT_SCHEMA,
            "event_id": f"evt-{uuid.uuid4().hex[:16]}",
            "event_type": event_type,
            "logical_session_id": session.logical_session_id,
            "task_id": session.task_id,
            "channel": session.channel.value,
            "provider": session.provider.value if session.provider else None,
            "created_at": _now(),
            "payload": dict(payload),
        }
        _, event_path, _ = self._paths(session.logical_session_id)
        with append_text(event_path) as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def create(self, session: LogicalSession) -> None:
        """Create a new snapshot and its first event; refuse accidental overwrite."""
        snapshot_path, _, _ = self._paths(session.logical_session_id)
        with self._locked(session.logical_session_id):
            if snapshot_path.exists():
                raise FileExistsError(f"logical session already exists: {session.logical_session_id}")
            self._save_unlocked(session)
            self._append_event_unlocked(session, "session_created", {"status": session.status.value})

    def load(self, session_id: str) -> LogicalSession:
        snapshot_path, _, _ = self._paths(session_id)
        try:
            payload = json.loads(read_text(snapshot_path))
        except FileNotFoundError:
            raise FileNotFoundError(f"logical session not found: {session_id}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"logical session snapshot is corrupt: {session_id}") from exc
        return load_logical_session(payload)

    @staticmethod
    def _activity_key(session: LogicalSession) -> tuple[float, str, str]:
        """Use persisted activity metadata, never filesystem mtime."""
        try:
            activity = datetime.fromisoformat(session.updated_at.replace("Z", "+00:00")).timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            activity = 0.0
        return activity, session.updated_at or "", session.logical_session_id

    def list_sessions(
        self,
        *,
        provider: Provider | str | None = None,
        channel: SessionChannel | str | None = None,
        workspace: str = "",
        include_failed: bool = True,
    ) -> list[LogicalSession]:
        """List snapshots ordered by explicit ``updated_at`` metadata."""
        ensure_private_directory(self.root)
        selected_provider = Provider(provider).value if provider is not None else ""
        selected_channel = SessionChannel(channel).value if channel is not None else ""
        sessions: list[LogicalSession] = []
        snapshot_root = self.root / "snapshots"
        try:
            paths = sorted(snapshot_root.glob("*.json"), key=lambda item: item.name)
        except OSError:
            paths = []
        for path in paths:
            try:
                session = self.load(path.stem)
            except (OSError, UnicodeError, ValueError):
                continue
            if selected_provider and (session.provider.value if session.provider else "") != selected_provider:
                continue
            if selected_channel and session.channel.value != selected_channel:
                continue
            if workspace and session.workspace != workspace and session.worktree != workspace:
                continue
            if not include_failed and session.status in {SessionStatus.FAILED, SessionStatus.CANCELLED}:
                continue
            sessions.append(session)
        return sorted(sessions, key=self._activity_key, reverse=True)

    def latest_session(self, **filters: Any) -> LogicalSession | None:
        """Return the one latest session by ``updated_at`` and stable ID tie-break."""
        sessions = self.list_sessions(**filters)
        return sessions[0] if sessions else None

    def append_event(self, session_id: str, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._locked(session_id):
            session = self.load(session_id)
            session.updated_at = _now()
            self._save_unlocked(session)
            return self._append_event_unlocked(session, event_type, payload)

    def save(self, session: LogicalSession, *, event_type: str = "", payload: Mapping[str, Any] | None = None) -> None:
        """Atomically save an existing snapshot and optionally append one event."""
        snapshot_path, _, _ = self._paths(session.logical_session_id)
        with self._locked(session.logical_session_id):
            if not snapshot_path.exists():
                raise FileNotFoundError(f"logical session not found: {session.logical_session_id}")
            session.updated_at = _now()
            self._save_unlocked(session)
            if event_type:
                self._append_event_unlocked(session, event_type, payload or {})

    def record_handoff(
        self,
        session_id: str,
        *,
        target_channel: SessionChannel | str,
        summary: str,
        next_action: str = "",
        target_provider: str | None = None,
        reason: str = "channel handoff",
    ) -> LogicalSession:
        """Atomically update the bounded handoff snapshot and append its event."""
        target = SessionChannel(target_channel)
        if _contains_sensitive({"summary": summary, "next_action": next_action, "reason": reason}):
            raise ValueError("handoff contains a sensitive marker")
        with self._locked(session_id):
            session = self.load(session_id)
            session.channel = target
            session.summary = str(summary)
            session.next_action = str(next_action)
            session.status = SessionStatus.HANDOFF_READY
            if target_provider is not None:
                session.provider = Provider(target_provider)
            session.updated_at = _now()
            if len(session.summary) > 8000:
                raise ValueError("handoff summary exceeds the logical-session limit")
            self._save_unlocked(session)
            self._append_event_unlocked(
                session,
                "session_handoff",
                {"target_channel": target.value, "reason": reason, "next_action": session.next_action},
            )
            return session

    def events(self, session_id: str) -> list[dict[str, Any]]:
        _, event_path, _ = self._paths(session_id)
        if not event_path.exists():
            return []
        events = []
        for line in read_text(event_path).splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"context event journal is corrupt: {session_id}") from exc
            if event.get("schema") != EVENT_SCHEMA:
                raise ValueError(f"unsupported context event schema: {event.get('schema')!r}")
            events.append(event)
        return events

    def bounded_context(self, session_id: str) -> str:
        """Render a bounded, non-transcript handoff block for a provider prompt."""
        session = self.load(session_id)
        lines = [
            "[엣지 에이전트 공유 작업 컨텍스트]",
            f"논리 세션: {session.logical_session_id}",
            f"작업: {session.task_id}",
            f"상태: {session.status.value}",
            f"요약: {session.summary or '(없음)'}",
            f"다음 작업: {session.next_action or '(미정)'}",
            "결정사항:",
            *[f"- {item}" for item in session.decisions[-10:]],
            "위험 메모:",
            *[f"- {item}" for item in session.risk_notes[-10:]],
            "변경 파일:",
            *[f"- {item}" for item in session.changed_files[-50:]],
            f"검증: {json.dumps(session.verification, ensure_ascii=False, sort_keys=True)}",
            "주의: 이 블록은 참고용 요약이며 provider native 세션을 병합하지 않는다.",
        ]
        rendered = "\n".join(lines)
        if len(rendered) <= self.max_context_chars:
            return rendered
        return rendered[: self.max_context_chars - 30].rstrip() + "\n…(컨텍스트 축약)…"
