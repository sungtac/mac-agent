#!/usr/bin/env python3
from pathlib import Path

from _quota_resume_test_helpers import temp_workspace
from skills.quota_resume import quota_resume, supervisor_checkpoint_hook


def main() -> int:
    with temp_workspace() as td:
        base = Path(td)
        result = supervisor_checkpoint_hook.record_supervisor_checkpoint(base, {"task_id": "CHK1", "objective": "checkpoint"})
        assert result["status"] == "success"
        assert quota_resume.load_active_task(base)["task_id"] == "CHK1"
    print("PASS: test_supervisor_checkpoint_hook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
