#!/usr/bin/env python3
from pathlib import Path

from _quota_resume_test_helpers import temp_workspace
from skills.quota_resume import quota_resume, quota_resume_wrapper


def main() -> int:
    with temp_workspace() as td:
        base = Path(td)
        quota_resume.save_active_task(base, {"task_id": "TASK1", "objective": "resume safely"})
        quota_resume.add_resume_queue_item(base, {"task_id": "TASK1", "event_id": "EV1", "resume_after": "2000-01-01T00:00:00"})
        result = quota_resume_wrapper.build_ready_resume_previews(base)
        assert result["status"] == "success"
        assert result["preview_count"] == 1
        preview = result["previews"][0]
        assert preview["auto_execute"] is False
        assert preview["requires_user_review"] is True
        assert "TASK1" in quota_resume_wrapper.render_resume_list(base)
        assert "requires_user_review: true" in quota_resume_wrapper.render_resume_preview(base, "TASK1")
    print("PASS: test_quota_resume_wrapper")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
