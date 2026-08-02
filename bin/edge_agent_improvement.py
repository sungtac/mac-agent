#!/usr/bin/env python3
"""Durable improvement tasks created from newly discovered blocking facts."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    from edge_agent_secure_paths import ensure_private_directory, open_lock, read_text
except ModuleNotFoundError:  # direct import by the harness test loader
    import importlib.util
    import sys

    _SECURE_PATHS = Path(__file__).with_name("edge_agent_secure_paths.py")
    _SECURE_SPEC = importlib.util.spec_from_file_location("edge_agent_secure_paths", _SECURE_PATHS)
    if _SECURE_SPEC is None or _SECURE_SPEC.loader is None:
        raise
    _SECURE_MODULE = importlib.util.module_from_spec(_SECURE_SPEC)
    sys.modules[_SECURE_SPEC.name] = _SECURE_MODULE
    _SECURE_SPEC.loader.exec_module(_SECURE_MODULE)
    ensure_private_directory = _SECURE_MODULE.ensure_private_directory
    open_lock = _SECURE_MODULE.open_lock
    read_text = _SECURE_MODULE.read_text


SCHEMA = "edge_agent.improvement_task.v1"
MAX_SUMMARY_CHARS = 800
MAX_EVIDENCE_CHARS = 500
MAX_NEXT_ACTION_CHARS = 800
MAX_ACCEPTANCE_CHARS = 800
_CATEGORIES = {"capability", "usage", "process", "workspace", "test", "security", "integration", "runtime"}
_SENSITIVE = re.compile(
    r"(?i)(?:token\s*[:=]|api[_ -]?key\s*[:=]|authorization\s*[:=]|bearer\s+|"
    r"password\s*[:=]|cookie\s*[:=]|secret\s*[:=]|private[_ -]?key\s*[:=]|\bsk-[A-Za-z0-9_-]{8,})"
)


class ImprovementError(ValueError):
    """Raised when a discovered fact cannot become a safe improvement task."""


def _root() -> Path:
    return Path(os.environ.get("EDGE_AGENT_IMPROVEMENT_ROOT", str(Path.home() / ".edge-agent" / "improvements"))).expanduser()


def _text(value: object, field: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ImprovementError(f"{field} is required")
    if len(text) > limit:
        raise ImprovementError(f"{field} exceeds {limit} characters")
    if _SENSITIVE.search(text):
        raise ImprovementError(f"{field} contains sensitive material")
    return text


def _evidence(values: object) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)) or not values:
        raise ImprovementError("evidence is required")
    result = [_text(value, "evidence", MAX_EVIDENCE_CHARS) for value in values[:8]]
    if len(result) != len(values):
        raise ImprovementError("evidence exceeds 8 items")
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_task(
    *,
    source: str,
    category: str,
    summary: str,
    evidence: object,
    next_action: str,
    acceptance: str,
) -> dict[str, Any]:
    source_text = _text(source, "source", 240)
    category_text = _text(category, "category", 40).casefold()
    if category_text not in _CATEGORIES:
        raise ImprovementError(f"unsupported category: {category_text}")
    summary_text = _text(summary, "summary", MAX_SUMMARY_CHARS)
    evidence_list = _evidence(evidence)
    next_action_text = _text(next_action, "next_action", MAX_NEXT_ACTION_CHARS)
    acceptance_text = _text(acceptance, "acceptance", MAX_ACCEPTANCE_CHARS)
    fingerprint = json.dumps(
        {
            "source": source_text,
            "category": category_text,
            "summary": summary_text,
            "evidence": evidence_list,
            "next_action": next_action_text,
            "acceptance": acceptance_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    task_id = "improve-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    return {
        "schema": SCHEMA,
        "task_id": task_id,
        "source": source_text,
        "category": category_text,
        "status": "queued",
        "summary": summary_text,
        "evidence": evidence_list,
        "next_action": next_action_text,
        "acceptance": acceptance_text,
        "discovered_at": _now(),
    }


class ImprovementStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = ensure_private_directory(Path(root).expanduser() if root else _root())
        self.path = self.root / "tasks.jsonl"
        self.lock_path = self.root / "tasks.lock"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = open_lock(self.lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in read_text(self.path).splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
                raise ImprovementError("improvement task ledger is malformed")
            records.append(payload)
        return records

    def append(self, task: dict[str, Any]) -> str:
        if task.get("schema") != SCHEMA or not task.get("task_id"):
            raise ImprovementError("invalid improvement task")
        with self._locked():
            records = self._records()
            for existing in records:
                if existing.get("task_id") != task.get("task_id"):
                    continue
                comparable_existing = {key: value for key, value in existing.items() if key != "discovered_at"}
                comparable_task = {key: value for key, value in task.items() if key != "discovered_at"}
                if comparable_existing == comparable_task:
                    return "duplicate"
                raise ImprovementError("improvement task idempotency conflict")
            descriptor, temporary = tempfile.mkstemp(prefix=".tasks.", dir=self.root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    for record in records:
                        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.write(json.dumps(task, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            return "recorded"

    def mark_completed(self, task_id: str, revalidation_evidence: object) -> str:
        """Atomically close a queued improvement task after revalidation."""
        task_id_text = _text(task_id, "task_id", 120)
        evidence = _evidence(revalidation_evidence)
        with self._locked():
            records = self._records()
            target = next((item for item in records if item.get("task_id") == task_id_text), None)
            if target is None:
                raise ImprovementError("improvement task was not found")
            if target.get("status") == "completed":
                if target.get("revalidation_evidence") == evidence:
                    return "duplicate"
                raise ImprovementError("completed task evidence conflicts")
            target["status"] = "completed"
            target["revalidation_evidence"] = evidence
            target["completed_at"] = _now()
            descriptor, temporary = tempfile.mkstemp(prefix=".tasks.", dir=self.root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    for record in records:
                        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            return "completed"


def record_blocker(*, root: str | os.PathLike[str] | None = None, **kwargs: object) -> tuple[dict[str, Any], str]:
    task = build_task(**kwargs)  # type: ignore[arg-type]
    return task, ImprovementStore(root).append(task)


def mark_completed(task_id: str, revalidation_evidence: object, *, root: str | os.PathLike[str] | None = None) -> str:
    return ImprovementStore(root).mark_completed(task_id, revalidation_evidence)


def improvement_for_result(result: dict[str, Any], *, source: str, root: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any], str]:
    error = str(result.get("error") or "workflow_blocked")
    issues = result.get("blocking_issues")
    evidence = [f"result_error={error}"]
    if isinstance(issues, list):
        evidence.append(f"blocking_issue_count={len(issues)}")
    task, outcome = record_blocker(
        root=root,
        source=source,
        category="process",
        summary=f"검증 결과가 차단됨: {error}",
        evidence=evidence,
        next_action="차단 원인을 확인하고 최소 범위의 개선을 구현한 뒤 동일 하네스 검증을 재실행한다.",
        acceptance="개선 작업이 queued/in_progress 상태로 기록되고 재검증 증거가 남을 때까지 원래 작업은 blocked로 유지한다.",
    )
    return task, outcome


__all__ = ["SCHEMA", "ImprovementError", "ImprovementStore", "build_task", "record_blocker", "mark_completed", "improvement_for_result"]
