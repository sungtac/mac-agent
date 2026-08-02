#!/usr/bin/env python3
"""Cross-process Telegram egress gate with bounded backpressure.

The provider outbox remains the durable source of response chunks.  This
module serializes the actual Telegram send across independent LaunchAgents,
enforces a per-chat minimum interval, and fails closed when the bounded wait
window is exhausted.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from edge_agent_secure_paths import ensure_private_directory


DEFAULT_ROOT = Path(
    os.environ.get(
        "EDGE_AGENT_TELEGRAM_EGRESS_ROOT",
        str(Path.home() / ".edge-agent" / "state" / "telegram-egress"),
    )
).expanduser()
DEFAULT_INTERVAL_SECONDS = max(
    0.01, float(os.environ.get("EDGE_AGENT_TELEGRAM_EGRESS_INTERVAL_SECONDS", "0.20"))
)
DEFAULT_WAIT_SECONDS = max(
    0.1, float(os.environ.get("EDGE_AGENT_TELEGRAM_EGRESS_WAIT_SECONDS", "8"))
)


class EgressQueueError(RuntimeError):
    """The shared egress queue could not provide a bounded send slot."""


class EgressPermit:
    def __init__(self, descriptor: int, sequence: int, chat_id: object):
        self.descriptor = descriptor
        self.sequence = sequence
        self.chat_id = str(chat_id)
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
            self._released = True

    def __enter__(self) -> "EgressPermit":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class SharedEgressQueue:
    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
    ):
        self.root = ensure_private_directory(Path(root).expanduser() if root else DEFAULT_ROOT)
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.wait_seconds = max(0.1, float(wait_seconds))
        self.state_lock_path = self.root / ".state.lock"
        self.state_path = self.root / "queue.json"

    def _chat_lock_path(self, chat_id: object) -> Path:
        digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:24]
        return self.root / f".chat-{digest}.lock"

    def _with_state_lock(self):
        descriptor = os.open(self.state_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(self.state_lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        last_sent = payload.get("last_sent")
        payload["last_sent"] = last_sent if isinstance(last_sent, dict) else {}
        try:
            payload["sequence"] = int(payload.get("sequence") or 0)
        except (TypeError, ValueError):
            payload["sequence"] = 0
        return payload

    def _write_state(self, payload: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".egress-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            os.chmod(self.state_path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def acquire(
        self,
        chat_id: object,
        *,
        delivery_id: str = "",
        chunk_index: int = 0,
        wait_seconds: float | None = None,
    ) -> EgressPermit:
        """Reserve a serialized send slot or raise after bounded backpressure."""
        deadline = time.monotonic() + max(0.1, float(wait_seconds or self.wait_seconds))
        descriptor = os.open(self._chat_lock_path(chat_id), os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(self._chat_lock_path(chat_id), 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise EgressQueueError("Telegram egress queue backpressure limit reached")
                time.sleep(0.025)
        try:
            key = str(chat_id)
            state_descriptor = self._with_state_lock()
            try:
                payload = self._read_state()
                now = time.time()
                previous = float(payload["last_sent"].get(key) or 0.0)
                delay = max(0.0, self.interval_seconds - (now - previous))
            finally:
                fcntl.flock(state_descriptor, fcntl.LOCK_UN)
                os.close(state_descriptor)
            if time.monotonic() + delay > deadline:
                raise EgressQueueError("Telegram egress chat rate limit wait exceeded")
            if delay:
                time.sleep(delay)
                now = time.time()
            state_descriptor = self._with_state_lock()
            try:
                payload = self._read_state()
                sequence = int(payload["sequence"]) + 1
                payload["sequence"] = sequence
                payload["last_sent"][key] = now
                payload["last_delivery_id"] = str(delivery_id or "")[-200:]
                payload["last_chunk_index"] = int(chunk_index)
                payload["updated_epoch"] = now
                self._write_state(payload)
            finally:
                fcntl.flock(state_descriptor, fcntl.LOCK_UN)
                os.close(state_descriptor)
            return EgressPermit(descriptor, sequence, chat_id)
        except Exception:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            raise


__all__ = ["EgressPermit", "EgressQueueError", "SharedEgressQueue"]
