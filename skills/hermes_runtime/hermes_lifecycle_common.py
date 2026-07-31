#!/usr/bin/env python3
"""Shared Hermes lifecycle helpers.

This module is intentionally small and side-effect free.  It provides the common
record filtering and text extraction used by lifecycle, readiness, and active
resolution gates without mutating the Hermes ledger.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from skills.hermes_runtime.hermes_backlog import load_feedback_records, normalize_status, score_priority

HIGH_PRIORITY = 90
ACTIVE_STAGES = {"proposed", "planned", "blocked"}
RETIRABLE_STAGES = {"mitigated", "live_verified", "retired"}


def record_text(record: dict[str, Any], *keys: str, lowercase: bool = False) -> str:
    parts: list[str] = []
    for key in keys:
        value = record.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value if str(v).strip())
        elif value is not None and str(value).strip():
            parts.append(str(value))
    text = "\n".join(parts)
    return text.lower() if lowercase else text


def priority_of(record: dict[str, Any]) -> int:
    return max(score_priority(record), int(record.get("priority", 0) or 0))


def lifecycle_stage(record: dict[str, Any]) -> str:
    explicit = str(record.get("lifecycleStage") or record.get("lifecycle_stage") or "").strip().lower().replace("-", "_")
    if explicit:
        return explicit
    raw = str(record.get("status") or "proposed").strip().lower().replace("-", "_")
    if raw in {"retired", "live_verified", "static_verified"}:
        return raw
    if raw in {"implemented", "validated", "mitigated"}:
        return "mitigated"
    normalized = normalize_status(record).replace("-", "_")
    if normalized in {"retired", "live_verified", "static_verified"}:
        return normalized
    if normalized in {"implemented", "validated", "mitigated"}:
        return "mitigated"
    return normalized or raw or "proposed"


def high_priority_records(path: str | Path, *, threshold: int = HIGH_PRIORITY) -> list[tuple[int, dict[str, Any], int, str]]:
    records = load_feedback_records(path)
    out: list[tuple[int, dict[str, Any], int, str]] = []
    for idx, record in enumerate(records):
        priority = priority_of(record)
        if priority >= threshold:
            out.append((idx, record, priority, lifecycle_stage(record)))
    return out


def records_in_stages(path: str | Path, stages: Iterable[str], *, threshold: int = HIGH_PRIORITY) -> list[tuple[int, dict[str, Any], int, str]]:
    wanted = {stage.replace("-", "_") for stage in stages}
    return [(idx, record, priority, stage) for idx, record, priority, stage in high_priority_records(path, threshold=threshold) if stage in wanted]


def raw_status(record: dict[str, Any], fallback: str = "proposed") -> str:
    return str(record.get("status") or fallback or "proposed")
