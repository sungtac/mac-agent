#!/usr/bin/env python3
from skills.quota_resume import telegram_notification_candidate as candidate


def main() -> int:
    bundle = candidate.build_notification_bundle({"task_id": "TG1", "objective": "review resume", "next_safe_steps": ["inspect", "approve"]})
    message = bundle["messages"][0]
    assert message["delivery_success"] is False
    assert message["message_id"] is None
    assert "auto_execute: false" in message["message_candidate"]
    assert "requires_user_review: true" in message["message_candidate"]
    assert candidate.assert_no_delivery_claim_without_message_id(message)["ok"] is True
    assert candidate.assert_no_delivery_claim_without_message_id({"message_candidate": "전송 완료", "message_id": None})["ok"] is False
    print("PASS: test_telegram_notification_candidate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
