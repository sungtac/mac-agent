#!/usr/bin/env python3
"""Persist preview-only supervisor checkpoints under the Edge Agent state root."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from skills.quota_resume import quota_resume


def save_checkpoint(base_dir: str | Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    return quota_resume.save_active_task(base_dir, checkpoint)


def record_supervisor_checkpoint(base_dir: str | Path, task: dict[str, Any]) -> dict[str, Any]:
    return quota_resume.save_active_task(base_dir, task)
