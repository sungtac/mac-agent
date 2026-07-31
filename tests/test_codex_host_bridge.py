from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCH = (ROOT / "workflows/lib/codex-execute-dispatch.sh").read_text()
HOST = (ROOT / "bin/codex-execute-host-job.sh").read_text()


def main():
    for marker in ("sandbox_apply", "launchctl submit", "HOST_JOB", "host Codex bridge"):
        assert marker in DISPATCH, marker
    for marker in ("workspace-write", "find_codex_bin", "STATUS_FILE", "/private/tmp/"):
        assert marker in HOST
    print("PASS: host-side Codex bridge contract present")


if __name__ == "__main__":
    main()
