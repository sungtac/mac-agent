#!/usr/bin/env python3
"""Small adapter shared by terminal and channel runtimes.

This is deliberately a bridge over the existing logical-session contract and
ContextStore.  It does not resume a provider-native session or store raw
transcripts.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from typing import Any

from edge_agent_context_store import ContextStore
from edge_agent_session_contract import LogicalSession, Provider, SessionChannel, SessionStatus


def _store() -> ContextStore:
    return ContextStore(os.environ.get("EDGE_AGENT_SESSION_ROOT") or None)


def session_id_for_task(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not value or any(char in value for char in "/\\\x00"):
        raise ValueError("task_id must be a safe non-empty value")
    return f"sess-{value}"


def start_session(*, task_id: str, channel: str, provider: str, owner: str,
                  workspace: str = "", worktree: str = "") -> str:
    session_id = session_id_for_task(task_id)
    store = _store()
    try:
        session = store.load(session_id)
        session.channel = SessionChannel(channel)
        session.provider = Provider(provider)
        session.owner = owner
        session.workspace = workspace
        session.worktree = worktree
        session.status = SessionStatus.RUNNING
        session.updated_at = datetime.now(timezone.utc).isoformat()
        store.save(session, event_type="session_resumed", payload={"owner": owner})
    except FileNotFoundError:
        session = LogicalSession(
            logical_session_id=session_id,
            task_id=task_id,
            channel=channel,
            provider=provider,
            owner=owner,
            workspace=workspace,
            worktree=worktree,
            status=SessionStatus.RUNNING,
        )
        store.create(session)
    return session_id


def update_session(session_id: str, *, status: str, summary: str = "",
                   next_action: str = "", workspace: str = "", worktree: str = "",
                   changed_files: list[str] | None = None,
                   verification: dict[str, Any] | None = None,
                   event_type: str = "session_updated") -> None:
    store = _store()
    session = store.load(session_id)
    session.status = SessionStatus(status)
    if summary:
        session.summary = summary[:8000]
    if next_action:
        session.next_action = next_action[:8000]
    if workspace:
        session.workspace = workspace
    if worktree:
        session.worktree = worktree
    if changed_files is not None:
        session.changed_files = changed_files[:500]
    if verification is not None:
        session.verification = verification
    store.save(session, event_type=event_type, payload={"status": session.status.value})


def bounded_context(session_id: str) -> str:
    return _store().bounded_context(session_id)


def finish_session(session_id: str, *, status: str, summary: str = "") -> None:
    update_session(
        session_id,
        status=status,
        summary=summary,
        next_action="사용자 후속 요청 대기" if status == "succeeded" else "실패 원인과 검증 결과 확인",
        event_type="terminal_provider_finished",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("context", "session-id", "start", "finish"))
    parser.add_argument("value")
    parser.add_argument("provider", nargs="?")
    parser.add_argument("workspace", nargs="?")
    parser.add_argument("status", nargs="?")
    args = parser.parse_args()
    if args.command == "context":
        print(bounded_context(args.value), end="")
    elif args.command == "session-id":
        print(session_id_for_task(args.value))
    elif args.command == "start":
        if not args.provider or not args.workspace:
            parser.error("start requires task_id provider workspace")
        provider = "antigravity" if args.provider == "agy" else args.provider
        print(start_session(task_id=args.value, channel="terminal", provider=provider,
                            owner="terminal", workspace=args.workspace, worktree=args.workspace))
    else:
        finish_status = args.status or args.provider
        if finish_status not in {"succeeded", "failed"}:
            parser.error("finish requires status succeeded|failed")
        finish_session(args.value, status=finish_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
