#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from _quota_resume_test_helpers import temp_workspace
from skills.quota_resume import fallback_switch_preview


def main() -> int:
    with temp_workspace() as td:
        base = Path(td)
        preview = fallback_switch_preview.build_fallback_switch_preview({"task_id": "FB1", "event_id": "EV1"})
        assert preview["ok"] is True
        assert preview["auto_execute"] is False
        assert preview["requires_user_review"] is True
        saved = fallback_switch_preview.save_fallback_switch_preview(base, preview)
        assert saved["status"] == "success"
        latest = fallback_switch_preview.latest_pending_preview(base)
        assert latest is not None
        assert latest["task_id"] == "FB1"
        assert latest["status"] == "pending_approval"
    print("PASS: test_fallback_switch_preview")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
