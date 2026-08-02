from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "phase1a-shadow-operations-runbook.md"


def section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    start = text.index(marker)
    rest = text[start + len(marker):]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def test_explicit_contracts() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assertions = {
        "flag_off": [
            "EDGE_AGENT_SHADOW_OBSERVER_ENABLED",
            "기본값",
            "OFF",
            "queue·worker·DB·JSONL",
            "재시작 후",
        ],
        "router_claim_disabled": [
            "중앙 Task Router",
            "비활성",
            "실행 Claim·Lease",
            "실행 제어에 사용하지 않음",
            "Provider 선택·실행 차단",
        ],
        "permissions": [
            "Shadow root directory",
            "0700",
            "SQLite DB",
            "0600",
            "symlink",
            "현재 서비스 사용자",
        ],
        "commands": [
            "status",
            "retention-dry-run",
            "retention-execute",
            "purge-all-dry-run",
            "purge-all-execute",
            "verify",
        ],
        "hmac": [
            "HMAC-SHA256",
            "key_id",
            "body_hmac_key_id",
            "UNKNOWN",
            "SHA-256 fallback",
            "메시지마다 다시 읽지 않는다",
        ],
        "identity": [
            "root_task_id",
            "event_id",
            "HMAC rotation",
            "Task Identity",
            "body HMAC",
            "불변",
        ],
        "disk_recovery": [
            "HARD_LIMIT",
            "dropped_disk_budget",
            "Legacy Telegram",
            "사용자 승인을 받는다",
            "RECOVERING",
        ],
        "canary": [
            "Antigravity",
            "Codex와 Claude는 OFF",
            "Shadow worker 0",
            "설정 rollback",
            "코드 rollback",
        ],
    }
    lowered = text.casefold()
    for name, terms in assertions.items():
        assert all(term.casefold() in lowered for term in terms), name
    assert "retention-execute" in section(text, "6. SQLite retention")
    assert "사용자 승인" in section(text, "6. SQLite retention")
    assert "purge-all-dry-run" in section(text, "11. 삭제 안전 계약")


def test_no_secret_or_broad_delete_command() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    forbidden = [
        r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b",
        r"-----BEGIN .*PRIVATE KEY-----",
        r"\brm\s+-rf\b",
        r"\bchmod\s+-R\b",
        r"\bchown\s+-R\b",
        r"find\s+.+\s+-delete\b",
    ]
    assert not any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in forbidden)


def test_runtime_defaults_are_documented() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    runtime = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "bin/edge_agent_shadow_observer.py",
            "bin/edge_agent_shadow_maintenance.py",
            "bin/edge_agent_shadow_keyring.py",
        )
    )
    runtime_markers = (
        "EDGE_AGENT_SHADOW_OBSERVER_ENABLED",
        "retention_days: int = 30",
        "jsonl_retention_days: int = 14",
        "512 * 1024 * 1024",
        "256 * 1024 * 1024",
        "768 * 1024 * 1024",
        "1024 * 1024 * 1024",
        "rotation_days: int = 90",
        "body_hmac_key_id",
    )
    document_markers = (
        "30일",
        "14일",
        "512MB급",
        "256MB급",
        "768MB급",
        "1GB급",
        "90일",
    )
    for marker in runtime_markers:
        assert marker in runtime, marker
    for marker in document_markers:
        assert marker in text, marker


def test_identity_contract_does_not_bind_hmac() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "메시지 본문, body HMAC, key_id" in text
    assert "task_id = hash(body_hmac)" in text
    assert "event_id = hash(key_id + body_hmac)" in text
    assert "HMAC rotation으로 cross-bot dedup 결과가 달라지지 않는다" in text
