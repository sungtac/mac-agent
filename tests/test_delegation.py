from __future__ import annotations

import tempfile
import unittest
import os
from unittest.mock import patch

from edge_agent_delegation import (
    DelegationStore,
    assignment_for_request,
    delegation_id_for,
    is_online_search_request,
    public_search_unavailable_message,
)


class DelegationTests(unittest.TestCase):
    def test_role_matrix_is_conservative_and_deterministic(self):
        self.assertEqual(assignment_for_request("코덱스야 코드 리뷰해줘")["target_role"], "claude")
        self.assertEqual(assignment_for_request("보안 취약점과 권한 우회를 점검해줘")["target_role"], "antigravity")
        self.assertEqual(assignment_for_request("이 문서를 요약해줘")["target_role"], "roda")
        self.assertIsNone(assignment_for_request("코드 구현하고 테스트 작성해줘"))
        self.assertIsNone(assignment_for_request("이거 어떻게 할지 봐줘"))

    def test_live_search_requires_a_verified_adapter_before_antigravity_routing(self):
        self.assertTrue(is_online_search_request("온라인에서 이력서 양식 찾아줘"))
        self.assertIsNone(assignment_for_request("온라인에서 이력서 양식 찾아줘"))
        with patch.dict(
            os.environ,
            {
                "EDGE_AGENT_PUBLIC_SEARCH_ENABLED": "1",
                "EDGE_AGENT_PUBLIC_SEARCH_ADAPTER": "verified-public-search-v1",
            },
            clear=False,
        ):
            self.assertEqual(
                assignment_for_request("온라인에서 이력서 양식 찾아줘")["target_role"],
                "antigravity",
            )
        self.assertIn("검증된 웹 검색 capability", public_search_unavailable_message())

    def test_named_storm_progress_is_a_search_request(self):
        request = "돌핀 태풍의 진행 상황 알려줘"
        self.assertTrue(is_online_search_request(request))
        self.assertIsNone(assignment_for_request(request))
        with patch.dict(
            os.environ,
            {
                "EDGE_AGENT_PUBLIC_SEARCH_ENABLED": "1",
                "EDGE_AGENT_PUBLIC_SEARCH_ADAPTER": "verified-public-search-v1",
            },
            clear=False,
        ):
            self.assertEqual(assignment_for_request(request)["target_role"], "antigravity")
            self.assertEqual(assignment_for_request(request)["scope"], "web")

    def test_search_words_inside_pasted_history_do_not_trigger_live_search(self):
        request = (
            "코덱스야 아래 답변의 문제를 분석해줘. "
            "[2026-08-08 오후 10:15] 로다: 웹 검색 capability가 없습니다."
        )
        self.assertFalse(is_online_search_request(request))
        self.assertIsNone(assignment_for_request(request))

    def test_queue_claim_complete_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DelegationStore(directory)
            delegation_id = delegation_id_for(root_task_id="telegram-1", target_role="claude")
            store.create(
                delegation_id,
                root_task_id="telegram-1",
                source_role="codex",
                target_role="claude",
                chat_id="-100",
                reply_to_message_id=7,
                request="코드 리뷰 token=do-not-store",
                reason="리뷰",
                acceptance_criteria="diff 확인",
                review_files=("src/example.py",),
                review_root="/Users/edge_ai/.edge-agent-worktrees/telegram-codex",
            )
            claimed = store.claim_for_role("claude", owner="test")
            self.assertIsNotNone(claimed)
            self.assertNotIn("do-not-store", str(claimed))
            self.assertEqual(claimed["review_files"], ["src/example.py"])
            result = store.complete(delegation_id, status="completed", response="검토 결과")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(store.wait(delegation_id, timeout_seconds=0)["response"], "검토 결과")

    def test_other_roles_cannot_claim_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DelegationStore(directory)
            delegation_id = delegation_id_for(root_task_id="telegram-2", target_role="roda")
            store.create(
                delegation_id,
                root_task_id="telegram-2",
                source_role="codex",
                target_role="roda",
                chat_id="-100",
                reply_to_message_id=None,
                request="요약해줘",
                reason="정리",
                acceptance_criteria="제공된 자료만 사용",
            )
            self.assertIsNone(store.claim_for_role("claude"))
            self.assertIsNotNone(store.claim_for_role("roda"))

    def test_cancel_prevents_a_late_consumer_from_marking_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DelegationStore(directory)
            delegation_id = delegation_id_for(root_task_id="telegram-3", target_role="claude")
            store.create(
                delegation_id,
                root_task_id="telegram-3",
                source_role="codex",
                target_role="claude",
                chat_id="-101",
                reply_to_message_id=None,
                request="코드 리뷰해줘",
                reason="리뷰",
                acceptance_criteria="diff 확인",
            )
            self.assertEqual(store.cancel_chat("-101", reason="취소"), 1)
            self.assertEqual(store.complete(delegation_id, status="completed")["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
