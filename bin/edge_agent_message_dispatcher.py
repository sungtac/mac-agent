#!/usr/bin/env python3
"""Bounded consumer for the durable peer-message bus.

The dispatcher deliberately accepts a handler callback instead of importing a
provider.  Telegram, terminal, and future adapters can therefore share the
same claim/checkpoint/ack lifecycle without sharing I/O code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from edge_agent_agent_message import AgentMessage
from edge_agent_message_bus import MessageBus, MessageBusError, message_from_dict


@dataclass(frozen=True)
class DispatchOutcome:
    summary: str = ""
    child_messages: tuple[AgentMessage, ...] = ()


Handler = Callable[[AgentMessage], DispatchOutcome | str | None]


class MessageDispatcher:
    """Process a finite batch; never spin indefinitely inside one invocation."""

    def __init__(self, bus: MessageBus | None = None):
        self.bus = bus or MessageBus()

    def dispatch_once(
        self,
        role: str,
        handler: Handler,
        *,
        owner: str | None = None,
        session_id: str | None = None,
        limit: int = 4,
    ) -> dict[str, int]:
        owner = owner or f"{role}-dispatcher"
        claims = self.bus.claim(role, session_id=session_id, owner=owner, limit=limit)
        counters = {"claimed": len(claims), "completed": 0, "requeued": 0, "failed": 0}
        for item in claims:
            message = message_from_dict(item["message"])
            message_id = str(item["message_id"])
            self.bus.checkpoint(message.session_id, message.task_id, "dispatch", "claimed", summary=message.purpose)
            try:
                outcome = handler(message)
                if outcome is None:
                    normalized = DispatchOutcome()
                elif isinstance(outcome, str):
                    normalized = DispatchOutcome(summary=outcome)
                else:
                    normalized = outcome
                for child in normalized.child_messages:
                    self.bus.publish(child)
                self.bus.checkpoint(
                    message.session_id,
                    message.task_id,
                    "dispatch",
                    "completed",
                    summary=normalized.summary,
                )
                self.bus.acknowledge(message.session_id, message_id, owner=owner)
                counters["completed"] += 1
            except Exception as exc:
                retry = self.bus.release(
                    message.session_id,
                    message_id,
                    owner=owner,
                    error=f"{type(exc).__name__}: {exc}",
                    requeue=True,
                )
                try:
                    self.bus.checkpoint(
                        message.session_id,
                        message.task_id,
                        "dispatch",
                        "failed",
                        summary=f"{type(exc).__name__}: {exc}",
                    )
                except MessageBusError:
                    pass
                if retry == "queued":
                    counters["requeued"] += 1
                else:
                    counters["failed"] += 1
        return counters


__all__ = ["DispatchOutcome", "MessageDispatcher"]
