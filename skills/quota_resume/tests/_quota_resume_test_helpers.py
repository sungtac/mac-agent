from __future__ import annotations

import sys
import tempfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))
JARVIS = WORKSPACE / "jarvis"
if str(JARVIS) not in sys.path:
    sys.path.insert(0, str(JARVIS))


def temp_workspace():
    return tempfile.TemporaryDirectory()


def assert_preview_safety(payload):
    assert payload.get("auto_execute") is False, payload
    assert payload.get("requires_user_review") is True, payload
