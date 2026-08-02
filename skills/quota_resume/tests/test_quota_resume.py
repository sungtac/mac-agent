#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from _quota_resume_test_helpers import temp_workspace
from skills.quota_resume import quota_resume


def main() -> int:
    with temp_workspace() as td:
        base = Path(td)
        init = quota_resume.ensure_state_files(base)
        assert init["status"] == "success"
        for name in quota_resume.STATE_FILES:
            assert (base / "state" / name).exists(), name
        event = quota_resume.record_quota_event(base, {"message": "token=secret-value 429 rate_limit", "api_key": "sk-test"})
        assert event["status"] == "success"
        raw = (base / "state" / "quota_events.json").read_text(encoding="utf-8")
        assert "secret-value" not in raw
        assert "sk-test" not in raw
        queue = quota_resume.add_resume_queue_item(base, {"task_id": "T1", "resume_after": "2000-01-01T00:00:00"})
        assert queue["status"] == "success"
        duplicate = quota_resume.record_quota_event(base, {"event_id": event["event_id"], "message": "different"})
        assert duplicate["event"]["event_id"] == event["event_id"]
        assert len(json.loads((base / "state" / "quota_events.json").read_text(encoding="utf-8"))["events"]) == 1
        items = quota_resume.ready_queue_items(base)
        assert items and items[0]["auto_execute"] is False and items[0]["requires_user_review"] is True
        json.loads(raw)
    print("PASS: test_quota_resume")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
