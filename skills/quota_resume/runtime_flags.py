#!/usr/bin/env python3
"""Runtime flags for quota resume flows."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_FLAGS = {
    "autoloop_resume_apply_enabled": True,
    "quota_resume_auto_execute": False,
    "requires_user_review": True,
}


def load_flags(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        path = Path(__file__).resolve().parents[2] / "state" / "runtime_flags.json"
    p = Path(path)
    data = {}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    merged = dict(DEFAULT_FLAGS)
    merged.update(data)
    merged["quota_resume_auto_execute"] = False
    merged["requires_user_review"] = True
    return merged


def get_flag(name: str, default: Any = None) -> Any:
    return load_flags().get(name, default)
