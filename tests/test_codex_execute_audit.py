"""Codex 실행 감사 로그 계약의 정적 회귀 검사."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "workflows/lib/codex-execute-dispatch.sh").read_text()


def main():
    required = (
        "edge_agent_codex_execute.v1",
        "record_audit \"provider_failed\"",
        "record_audit \"provider_completed\"",
        "messageTail",
        "authorization:",
        "EMAIL_REDACTED",
    )
    for marker in required:
        assert marker in SOURCE, marker
    print("PASS: Codex execution audit and redaction contract present")


if __name__ == "__main__":
    main()
