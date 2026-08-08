#!/usr/bin/env python3
"""Durable, bounded Codex-to-peer delegation queue.

The Telegram bots cannot consume one another's Bot API messages reliably.  A
small private queue is therefore the transport for *work*, while Telegram is
used for the visible progress and result messages.  This module deliberately
contains no provider or Telegram dependency.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from edge_agent_secure_paths import ensure_private_directory, open_lock, read_text
from edge_agent_ingress import routing_projection


ROLES = ("claude", "antigravity", "roda")
TERMINAL_STATUSES = frozenset({"completed", "failed", "delivery_pending", "cancelled"})
MAX_REQUEST_CHARS = 7000
MAX_RESPONSE_CHARS = 8000
MAX_REVIEW_FILES = 32
MAX_REVIEW_PATH_CHARS = 240
DEFAULT_WAIT_SECONDS = 300.0
MAX_WAIT_SECONDS = 600.0
LEASE_SECONDS = 900.0
_SECRET = re.compile(
    r"(?i)(token|api[_ -]?key|authorization|bearer|password|cookie|secret|private[_ -]?key)\s*[:=]\s*[^\s,;]+"
)
_TELEGRAM_TOKEN = re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b")
_ONLINE_SEARCH_MARKERS = (
    "온라인", "인터넷", "웹 검색", "웹검색", "검색해", "검색해서", "검색 결과",
    "사이트 찾아", "링크 찾아", "출처 찾아", "online", "web search", "look up",
)
_STORM_MARKERS = ("태풍", "허리케인", "사이클론", "열대저기압")
_STORM_STATUS_MARKERS = ("진행 상황", "진행상황", "현재 위치", "경로", "세력", "상륙", "발표", "예보", "상황")


def _root() -> Path:
    return Path(
        os.environ.get(
            "EDGE_AGENT_DELEGATION_ROOT",
            str(Path.home() / ".edge-agent" / "state" / "delegations"),
        )
    ).expanduser()


def _safe_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 180 or any(char in text for char in "/\\\x00"):
        raise ValueError("unsafe delegation id")
    return text


def _redact(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = _TELEGRAM_TOKEN.sub("[redacted-token]", text)
    text = _SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    if len(text) > limit:
        text = text[: limit - 12].rstrip() + "…(축약)"
    return text


def _safe_review_path(value: object) -> str:
    path = _redact(value, MAX_REVIEW_PATH_CHARS)
    if not path or path.startswith("/") or path in {".", ".."} or any(part == ".." for part in path.split("/")):
        raise ValueError("unsafe review file path")
    return path


def delegation_id_for(*, root_task_id: object, target_role: str) -> str:
    digest = hashlib.sha256(f"{root_task_id}|{target_role}".encode("utf-8")).hexdigest()[:24]
    return f"delegation-{digest}-{target_role}"


def _normalized(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def is_online_search_request(text: str) -> bool:
    normalized = _normalized(routing_projection(text))
    if any(marker.casefold() in normalized for marker in _ONLINE_SEARCH_MARKERS):
        return True
    return (
        any(marker.casefold() in normalized for marker in _STORM_MARKERS)
        and any(marker.casefold() in normalized for marker in _STORM_STATUS_MARKERS)
    )


def public_search_capability_available() -> bool:
    """Return true only when a separately verified search adapter is enabled.

    The current Telegram providers have no verified web-search adapter.  An
    environment flag alone is deliberately insufficient; deployment must
    identify the adapter contract explicitly before routing live search.
    """
    adapter_path = Path(__file__).with_name("edge_agent_public_search_adapter.py")
    return (
        os.environ.get("EDGE_AGENT_PUBLIC_SEARCH_ENABLED", "").strip().casefold() in {"1", "true", "yes", "on"}
        and os.environ.get("EDGE_AGENT_PUBLIC_SEARCH_ADAPTER", "").strip().casefold() == "verified-public-search-v1"
        and adapter_path.is_file()
    )


def public_search_unavailable_message() -> str:
    return (
        "⚠️ 현재 Telegram 팀에는 검증된 웹 검색 capability가 연결되어 있지 않습니다. "
        "확인하지 않은 링크나 검색 결과를 만들어내지 않았습니다. "
        "검색 capability를 연결하거나, 사용자가 찾은 링크를 보내주시면 내용을 검토·정리할 수 있습니다."
    )


def assignment_for_request(text: str) -> dict[str, str] | None:
    """Return the sole deterministic peer assignment, or None if ambiguous.

    The matrix is intentionally conservative.  A request may be delegated
    only when its dominant verb clearly matches one provider's contract.
    """
    normalized = _normalized(text)
    if not normalized:
        return None
    if is_online_search_request(text):
        if not public_search_capability_available():
            return None
        return {
            "target_role": "antigravity",
            "scope": "web",
            "reason": "실제 공개 웹 자료를 확인하는 독립 조사 역할",
            "acceptance_criteria": "검색한 URL·출처·확인 시각을 함께 제시하고, 확인하지 않은 링크나 사실은 만들지 않음",
        }
    security_markers = (
        "보안", "취약점", "위협", "red team", "red-team", "공격 경로",
        "권한 우회", "샌드박스", "비밀 유출", "security", "vulnerability",
    )
    review_markers = (
        "코드 리뷰", "코드리뷰", "코드 검토", "코드 검수", "코드 점검",
        "리뷰해", "검토해", "검수해", "검수 부탁", "review", "review 해",
    )
    preprocessing_markers = (
        "요약", "요약해", "추출", "정리해", "쉽게 설명", "가능성", "실현 가능",
        "타당성", " feasibility", "summarize", "extract",
    )
    implementation_markers = (
        "구현", "코드 작성", "코드 수정", "버그 수정", "리팩터", "리팩토링",
        "파일 수정", "만들어줘", "고쳐줘", "implement", "fix", "refactor",
    )
    if any(marker in normalized for marker in security_markers):
        return {
            "target_role": "antigravity",
            "scope": "files",
            "reason": "보안·반례·권한 경계를 독립적으로 검증하는 역할",
            "acceptance_criteria": "실제 확인한 증거, 재현 가능한 반례, 위험도와 남은 불확실성을 구분해 보고",
        }
    if any(marker in normalized for marker in review_markers):
        return {
            "target_role": "claude",
            "scope": "files",
            "reason": "팀 리더의 독립 코드 리뷰 역할",
            "acceptance_criteria": "실제 diff·관련 파일·테스트를 확인하고, 발견사항을 심각도와 근거와 함께 보고",
        }
    if any(marker in normalized for marker in preprocessing_markers):
        return {
            "target_role": "roda",
            "scope": "prompt",
            "reason": "자료 요약·추출·현실성 사전 정리 역할",
            "acceptance_criteria": "제공된 자료만 사용하고 확인하지 않은 실행·검색·완료를 주장하지 않음",
        }
    # Implementation stays with Codex as the deputy/precision implementer;
    # delegating it here would create a second writer and violate workspace
    # ownership.  Returning None makes Codex handle it directly.
    if any(marker in normalized for marker in implementation_markers):
        return None
    return None


class DelegationStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root).expanduser() if root is not None else _root()
        ensure_private_directory(self.root)

    def _path(self, delegation_id: str) -> Path:
        return self.root / f"{_safe_id(delegation_id)}.json"

    def _lock(self):
        return open_lock(self.root / ".store.lock")

    def _read(self, delegation_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(read_text(self._path(delegation_id)))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write(self, delegation_id: str, payload: Mapping[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=".delegation-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(dict(payload), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._path(delegation_id))
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def create(
        self,
        delegation_id: str,
        *,
        root_task_id: str,
        source_role: str,
        target_role: str,
        chat_id: object,
        reply_to_message_id: object | None,
        request: str,
        reason: str,
        acceptance_criteria: str,
        review_files: tuple[str, ...] = (),
        review_root: str = "",
    ) -> dict[str, Any]:
        if source_role != "codex" or target_role not in ROLES:
            raise ValueError("invalid delegation roles")
        delegation_id = _safe_id(delegation_id)
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = self._read(delegation_id)
            if current is not None:
                return current
            now = time.time()
            payload = {
                "schema": "edge_agent.delegation.v1",
                "delegation_id": delegation_id,
                "root_task_id": _redact(root_task_id, 180),
                "source_role": source_role,
                "target_role": target_role,
                "chat_id": str(chat_id),
                "reply_to_message_id": str(reply_to_message_id or ""),
                "request": _redact(request, MAX_REQUEST_CHARS),
                "reason": _redact(reason, 900),
                "acceptance_criteria": _redact(acceptance_criteria, 1200),
                "review_files": [
                    _safe_review_path(path)
                    for path in tuple(dict.fromkeys(review_files))[:MAX_REVIEW_FILES]
                ],
                "review_root": _redact(review_root, MAX_REVIEW_PATH_CHARS),
                "status": "queued",
                "created_epoch": now,
                "updated_epoch": now,
            }
            self._write(delegation_id, payload)
            return payload
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def claim_for_role(self, role: str, *, owner: str | None = None) -> dict[str, Any] | None:
        if role not in ROLES:
            raise ValueError("invalid delegation consumer role")
        now = time.time()
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            candidates = []
            for path in sorted(self.root.glob("delegation-*.json"), key=lambda item: item.name):
                try:
                    payload = json.loads(read_text(path))
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict) or payload.get("target_role") != role:
                    continue
                status = str(payload.get("status") or "")
                lease = float(payload.get("lease_until_epoch") or 0.0)
                if status == "queued" or (status == "processing" and lease <= now):
                    candidates.append(payload)
            if not candidates:
                return None
            payload = min(candidates, key=lambda item: float(item.get("created_epoch") or 0.0))
            delegation_id = _safe_id(payload.get("delegation_id"))
            payload["status"] = "processing"
            payload["claimed_by"] = _redact(owner or f"telegram-{role}", 180)
            payload["claimed_epoch"] = now
            payload["lease_until_epoch"] = now + LEASE_SECONDS
            payload["updated_epoch"] = now
            self._write(delegation_id, payload)
            return payload
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def complete(self, delegation_id: str, *, status: str, response: str = "", error: str = "", delivery_status: str = "unknown") -> dict[str, Any]:
        if status not in TERMINAL_STATUSES:
            raise ValueError("invalid delegation terminal status")
        delegation_id = _safe_id(delegation_id)
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = self._read(delegation_id)
            if payload is None:
                raise ValueError("delegation not found")
            if str(payload.get("status")) == "cancelled":
                return payload
            payload["status"] = status
            payload["response"] = _redact(response, MAX_RESPONSE_CHARS)
            payload["error"] = _redact(error, 1000)
            payload["delivery_status"] = _redact(delivery_status, 80)
            payload["completed_epoch"] = time.time()
            payload["updated_epoch"] = payload["completed_epoch"]
            self._write(delegation_id, payload)
            return payload
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def cancel_chat(self, chat_id: object, *, reason: str = "") -> int:
        """Cancel queued/processing work for one chat without deleting history."""
        target_chat = str(chat_id)
        cancelled = 0
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for path in sorted(self.root.glob("delegation-*.json"), key=lambda item: item.name):
                try:
                    payload = json.loads(read_text(path))
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict) or str(payload.get("chat_id")) != target_chat:
                    continue
                if str(payload.get("status")) not in {"queued", "processing"}:
                    continue
                payload["status"] = "cancelled"
                payload["cancel_reason"] = _redact(reason, 500)
                payload["updated_epoch"] = time.time()
                self._write(_safe_id(payload.get("delegation_id")), payload)
                cancelled += 1
            return cancelled
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def snapshot(self, delegation_id: str) -> dict[str, Any] | None:
        return self._read(_safe_id(delegation_id))

    def wait(self, delegation_id: str, *, timeout_seconds: float = DEFAULT_WAIT_SECONDS, interval_seconds: float = 0.5) -> dict[str, Any] | None:
        deadline = time.monotonic() + min(MAX_WAIT_SECONDS, max(0.0, timeout_seconds))
        while True:
            payload = self.snapshot(delegation_id)
            if payload and str(payload.get("status")) in TERMINAL_STATUSES:
                return payload
            if time.monotonic() >= deadline:
                return payload
            time.sleep(max(0.05, interval_seconds))


__all__ = [
    "DelegationStore",
    "DEFAULT_WAIT_SECONDS",
    "MAX_WAIT_SECONDS",
    "ROLES",
    "assignment_for_request",
    "delegation_id_for",
    "is_online_search_request",
    "public_search_capability_available",
    "public_search_unavailable_message",
]
