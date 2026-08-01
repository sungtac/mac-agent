import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCH = (ROOT / "workflows/lib/codex-execute-dispatch.sh").read_text()
HOST = (ROOT / "bin/codex-execute-host-job.sh").read_text()


class CodexHostBridgeTests(unittest.TestCase):
    def test_contract_markers(self):
        for marker in ("sandbox_apply", "launchctl submit", "HOST_JOB", "host Codex bridge"):
            self.assertIn(marker, DISPATCH)
        for marker in ("workspace-write", "find_codex_bin", "STATUS_FILE", "/private/tmp/"):
            self.assertIn(marker, HOST)

    def test_missing_prompt_is_rejected_before_codex_runs(self):
        with tempfile.TemporaryDirectory(prefix="codex-host-bridge-") as temp_dir:
            output_file = Path(temp_dir) / "output.log"
            status_file = Path(temp_dir) / "status"
            missing_prompt = Path(temp_dir) / "verify-task-v2-exec-XXXXXX.txt"
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(ROOT / "bin/codex-execute-host-job.sh"),
                    str(ROOT),
                    str(missing_prompt),
                    str(output_file),
                    str(status_file),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(status_file.read_text().strip(), "66")
            self.assertIn("프롬프트 파일을 찾을 수 없음", output_file.read_text())


def main():
    unittest.main()


if __name__ == "__main__":
    main()
