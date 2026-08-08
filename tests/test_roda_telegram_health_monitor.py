import importlib.util
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("health", Path(__file__).parents[1] / "bin" / "roda-telegram-health-monitor.py")
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


class RodaHealthMonitorTests(unittest.TestCase):
    def setUp(self):
        # poll_once() checks SOURCE_REPO's real git status for the
        # main_dirty signal; stub it so tests aren't at the mercy of this
        # repo's actual working-tree state. Tests exercising main_dirty
        # itself override this per-test.
        self._original_dirty_lines = health._source_repo_tracked_dirty_lines
        health._source_repo_tracked_dirty_lines = lambda: []

    def tearDown(self):
        health._source_repo_tracked_dirty_lines = self._original_dirty_lines

    def test_classifies_provider_failures_without_raw_prompt(self):
        self.assertEqual(health.classify_line("[codex] 빈 응답"), "empty_response")
        self.assertEqual(health.classify_line("[claude] 처리 실패 error=https://secret.example/x"), "execution_error")
        self.assertEqual(health.classify_line("[claude] claude exit=1:"), "execution_error")
        self.assertEqual(
            health.classify_line(
                "[claude] claude exit=1: Failed to authenticate: OAuth session expired and could not be refreshed"
            ),
            "auth_error",
        )
        self.assertEqual(
            health.classify_line("[claude] 처리 실패 error=Claude OAuth 인증이 만료되어 실행하지 못했습니다."),
            "auth_error",
        )
        self.assertEqual(health.classify_line('[claude] provider error type=rate_limit_error retry-after: 60'), "rate_limited")
        self.assertEqual(health.classify_line("[antigravity] RESOURCE_EXHAUSTED: quota exceeded"), "usage_limited")
        self.assertIsNone(health.classify_line("text='how should I handle rate limits?'"))
        self.assertIsNone(health.classify_line("text='what is a usage cap?'"))
        self.assertIsNone(health.classify_line("text='what does quota exceeded mean?'"))
        self.assertIsNone(health.classify_line("처리 완료 duration=3s"))
        self.assertIsNone(health.classify_line("[codex] run_polling 종료 — 프로세스 재시작을 위해 종료합니다."))
        self.assertNotIn("https://", health._safe_detail("error https://secret.example/x"))

    def test_auth_diagnostics_share_one_persistent_fingerprint(self):
        first = health._fingerprint("claude", "auth_error", "claude exit=1: OAuth expired")
        second = health._fingerprint("claude", "auth_error", "처리 실패 task=abc 인증 만료")
        self.assertEqual(first, second)

    def test_migration_coalesces_auth_and_supersedes_recent_generic_error(self):
        state = health._migrate_state({
            "schema_version": 4,
            "alerted": {"auth-a": 120, "auth-b": 121},
            "incidents": {
                "generic": {
                    "incident_id": "generic", "role": "claude", "code": "execution_error",
                    "status": "open", "first_seen_at": 100, "last_seen_at": 100,
                },
                "auth-a": {
                    "incident_id": "auth-a", "role": "claude", "code": "auth_error",
                    "status": "open", "first_seen_at": 120, "last_seen_at": 120,
                    "detail": "first",
                },
                "auth-b": {
                    "incident_id": "auth-b", "role": "claude", "code": "auth_error",
                    "status": "open", "first_seen_at": 121, "last_seen_at": 121,
                    "detail": "latest", "task_id": "task-1",
                },
            },
        })
        canonical = health._fingerprint("claude", "auth_error", "")
        self.assertEqual(state["schema_version"], 5)
        self.assertEqual(state["incidents"][canonical]["status"], "open")
        self.assertEqual(state["incidents"][canonical]["detail"], "latest")
        self.assertEqual(state["alerted"][canonical], 121)
        self.assertEqual(state["incidents"]["generic"]["status"], "superseded")
        self.assertEqual(state["incidents"]["auth-a"]["status"], "superseded")
        self.assertEqual(state["incidents"]["auth-b"]["status"], "superseded")

    def test_classifies_provider_usage_limit_variants_without_prompt_false_positive(self):
        self.assertEqual(health.classify_line("[claude] usage limit reached"), "usage_limited")
        self.assertEqual(health.classify_line("[claude] You've hit your session limit · resets 7pm"), "session_limited")
        self.assertEqual(health.classify_line("[claude] session limit reached"), "session_limited")
        self.assertEqual(health.classify_line("[codex] you have reached your usage limit"), "usage_limited")
        self.assertEqual(health.classify_line("usage cap"), "usage_limited")
        self.assertEqual(health.classify_line("[antigravity] RESOURCE_EXHAUSTED: limit exceeded"), "usage_limited")
        self.assertEqual(health.classify_line("[antigravity] RESOURCE_EXHAUSTED: capacity unavailable"), "capacity_limited")
        self.assertEqual(health.classify_line("[claude] overloaded_error"), "service_overloaded")
        self.assertEqual(health.classify_line("[codex] rate limit exceeded"), "rate_limited")
        self.assertEqual(health.classify_line("[claude] maximum context length exceeded"), "context_exceeded")
        self.assertIsNone(health.classify_line("text='please explain why the usage limit reached'"))
        self.assertIsNone(health.classify_line("provider=codex text='what does usage cap mean?'"))

    def test_safe_detail_redacts_credentials_and_request_id_is_hashed(self):
        line = "provider error Authorization: Bearer secret-token token=abc123 api_key=sk-live-value request_id=req-123"
        detail = health._safe_detail(line)
        self.assertNotIn("secret-token", detail)
        self.assertNotIn("abc123", detail)
        self.assertNotIn("sk-live-value", detail)
        self.assertEqual(len(health._request_id_hash(line)), 12)
        json_detail = health._safe_detail('{"api_key":"secret-json","authorization":"Bearer json-secret"}')
        self.assertNotIn("secret-json", json_detail)
        self.assertNotIn("json-secret", json_detail)

    def test_structured_error_is_preferred_over_message_regex(self):
        line = '{"error":{"type":"rate_limit_error","message":"quota exceeded"},"request_id":"req-123"}'
        event = health._usage_event("claude", "rate_limited", line, now=100)
        self.assertEqual(health.classify_line(line), "rate_limited")
        self.assertEqual(event["source"], "structured_error")
        self.assertEqual(event["authority"], "provider_response")
        self.assertIn("error.type=rate_limit_error", event["evidence"])
        self.assertEqual(len(event["request_id_hash"]), 12)

    def test_provider_structured_fixtures_use_provider_specific_mapping(self):
        fixtures = (
            ("claude", '{"error":{"type":"rate_limit_error"},"request_id":"req-claude"}', "rate_limited"),
            ("codex", '{"type":"usage_limit","message":"usage limit reached"}', "usage_limited"),
            ("antigravity", '{"status":"RESOURCE_EXHAUSTED","message":"capacity unavailable"}', "capacity_limited"),
        )
        for role, line, expected in fixtures:
            with self.subTest(role=role):
                self.assertEqual(health.classify_line(line, role=role), expected)

    def test_structured_redactor_recursively_removes_nested_secrets(self):
        line = '{"error":{"type":"rate_limit_error","details":{"api_key":"nested-secret"}},"metadata":[{"token":"nested-token"}]}'
        detail = health._safe_detail(line)
        self.assertNotIn("nested-secret", detail)
        self.assertNotIn("nested-token", detail)

    def test_arbitrary_json_cannot_be_promoted_to_provider_error(self):
        line = '{"message":"quota exceeded","metadata":{"accessToken":"raw-token","deploymentSecret":"raw-secret"}}'
        self.assertIsNone(health._structured_error(line))
        self.assertIsNone(health.classify_line(line, role="codex"))
        detail = health._safe_detail(line)
        self.assertNotIn("raw-token", detail)
        self.assertNotIn("raw-secret", detail)

        normal_event = '{"request_id":"req-123","message":"completed; usage quota metadata"}'
        self.assertIsNone(health._structured_error(normal_event))
        self.assertIsNone(health.classify_line(normal_event, role="codex"))

    def test_unstructured_usage_event_keeps_local_log_authority(self):
        event = health._usage_event("codex", "usage_limited", "quota exceeded", now=100)
        self.assertEqual(event["source"], "stderr")
        self.assertEqual(event["authority"], "local_log")

    def test_claude_session_limit_is_not_reported_as_account_usage_exhaustion(self):
        event = health._usage_event("claude", "session_limited", "You've hit your session limit · resets 7pm", now=100)
        self.assertIn("세션 상태 확인", event["message"])
        self.assertIn("계정 전체 사용량 제한으로 판정하지 않음", event["message"])
        self.assertNotIn("사용량 제한 감지", event["message"])
        self.assertEqual(event["auto_repair"], "blocked")

    def test_usage_event_extracts_exact_retry_after_without_guessing_window(self):
        event = health._usage_event(
            "claude",
            "rate_limited",
            "provider error rate_limit_error retry-after: 60",
            now=100,
        )
        self.assertEqual(event["reset_source"], "provider_retry_after")
        self.assertEqual(event["recovery_confidence"], "exact")
        self.assertIn("1970-01-01T00:02:40", event["reset_at"])
        self.assertIn("자동 Codex 복구: 실행하지 않음", event["message"])

    def test_usage_event_uses_window_without_fabricating_reset_time(self):
        event = health._usage_event("antigravity", "usage_limited", "baseline quota exhausted; weekly window", now=100)
        self.assertEqual(event["window"], "weekly")
        self.assertIsNone(event["reset_at"])
        self.assertEqual(event["recovery_confidence"], "estimated")
        self.assertIn("정확한 시각 확인 불가", event["message"])

    def test_usage_event_parses_compound_reset_duration(self):
        event = health._usage_event("codex", "rate_limited", "rate limit; resets in 1 hour 30 minutes", now=100)
        self.assertIsNone(event["retry_after_seconds"])
        self.assertEqual(event["recovery_confidence"], "estimated")
        self.assertEqual(event["authority"], "local_log")
        self.assertIn("1970-01-01T01:31:40", event["reset_at"])

    def test_usage_event_prefers_retry_after_and_parses_multiple_windows(self):
        event = health._usage_event(
            "antigravity",
            "usage_limited",
            "RESOURCE_EXHAUSTED retry-after: 60; 5-hour window and weekly limit exceeded",
            now=100,
        )
        self.assertEqual(event["reset_source"], "provider_retry_after")
        self.assertEqual(event["windows"], ["5h", "weekly"])
        self.assertEqual(event["confidence"], "exact")
        self.assertIn("5h, weekly", event["message"])
        self.assertIn("가용성 미확인", event["message"])

    def test_provider_capacity_does_not_fabricate_a_reset(self):
        event = health._usage_event("antigravity", "capacity_limited", "RESOURCE_EXHAUSTED: capacity unavailable", now=100)
        self.assertIsNone(event["reset_at"])
        self.assertEqual(event["recovery_confidence"], "unknown")
        self.assertIn("provider 용량 제한", event["message"])

        window_event = health._usage_event("antigravity", "usage_limited", "baseline quota exhausted; weekly window", now=100)
        self.assertEqual(window_event["authority"], "provider_policy")

    def test_state_migration_adds_schema_and_expires_legacy_usage_watch(self):
        original = health.STATE_FILE
        with tempfile.TemporaryDirectory() as td:
            health.STATE_FILE = Path(td) / "state.json"
            health.STATE_FILE.write_text(
                '{"initialized": true, "usage_watch": {"old": {"role": "codex", "created_at": 10}}}',
                encoding="utf-8",
            )
            state = health._load_state()
            self.assertEqual(state["schema_version"], health.STATE_SCHEMA_VERSION)
            self.assertGreater(state["usage_watch"]["old"]["expires_at"], 10)
            self.assertIn("metrics", state)
        health.STATE_FILE = original

    def test_legacy_fifo_pending_is_retired_without_hiding_incident_history(self):
        state = {
            "schema_version": 3,
            "pending": {"codex": [100]},
            "incidents": {
                "old": {
                    "incident_id": "old",
                    "role": "codex",
                    "task_id": "legacy-codex-0-100",
                    "status": "open",
                }
            },
        }
        health._migrate_state(state)
        self.assertEqual(state["pending"]["codex"], {})
        self.assertEqual(state["incidents"]["old"]["status"], "superseded")

    def test_unknown_and_parse_failure_metrics_are_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.log"
            log.write_text("기존 로그\n", encoding="utf-8")
            original_targets = health.TARGETS
            original_running = health._service_running
            health.TARGETS = {"codex": {"label": "present", "log": log}}
            health._service_running = lambda label: True
            try:
                state = {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}}
                health.poll_once(state, now=0)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write('provider {"error":{"type":"future_limit_error"}}\n')
                self.assertEqual(health.poll_once(state, now=1), [])
                self.assertEqual(state["metrics"]["unknown"], 1)
                self.assertEqual(state["metrics"]["parse_failures"], 1)
            finally:
                health.TARGETS = original_targets
                health._service_running = original_running

    def test_initial_poll_only_establishes_offsets(self):
        with tempfile.TemporaryDirectory() as td:
            original = health.TARGETS
            health.TARGETS = {"x": {"label": "missing", "log": Path(td) / "x.log"}}
            (Path(td) / "x.log").write_text("처리 실패 빈 응답\n", encoding="utf-8")
            state = {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}}
            self.assertEqual(health.poll_once(state), [])
            self.assertTrue(state["initialized"])
            health.TARGETS = original

    def test_recovery_watch_reports_reprocess_success(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.log"
            log.write_text("기존 로그\n", encoding="utf-8")
            original_targets = health.TARGETS
            original_running = health._service_running
            health.TARGETS = {"x": {"label": "present", "log": log}}
            health._service_running = lambda label: True
            state = {
                "initialized": False,
                "offsets": {},
                "pending": {},
                "alerted": {},
                "delivery_retry": [],
                "repair_results": {},
                "recovery_watch": {
                    "abc123": {
                        "role": "x",
                        "status": "awaiting_reprocess",
                        "deadline": 9999,
                        "notified": False,
                    }
                },
            }
            health.poll_once(state, now=100)
            with log.open("a", encoding="utf-8") as handle:
                handle.write("처리 시작 chat=test\n처리 완료 chat=test duration=1s\n")
            alerts = health.poll_once(state, now=101)
            self.assertEqual(state["recovery_watch"]["abc123"]["status"], "completed_success")
            self.assertEqual(alerts[0]["kind"], "recovery_result")
            health.TARGETS = original_targets
            health._service_running = original_running

    def test_isolated_end_to_end_detection_repair_and_recovery_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "x.log"
            state_file = root / "state.json"
            log.write_text("기존 로그\n", encoding="utf-8")
            original = {
                "TARGETS": health.TARGETS,
                "STATE_FILE": health.STATE_FILE,
                "_service_running": health._service_running,
                "_run_codex_repair": health._run_codex_repair,
                "_repair_preflight_blocker": health._repair_preflight_blocker,
                "_send_alert": health._send_alert,
            }
            alerts = []
            repairs = []
            health.TARGETS = {"x": {"label": "present", "log": log}}
            health.STATE_FILE = state_file
            health._service_running = lambda label: True
            health._run_codex_repair = lambda event, state: repairs.append(event["code"]) or "Codex 자동 수정·main 병합·x 서비스 재기동 완료."
            health._repair_preflight_blocker = lambda event: None
            health._send_alert = alerts.append
            try:
                state = {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}, "delivery_retry": [], "repair_results": {}, "recovery_watch": {}}
                health._process_cycle(state)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 실패 빈 응답\n")
                health._process_cycle(state)
                self.assertEqual(repairs, ["empty_response"])
                self.assertEqual(len(alerts), 2)
                self.assertIn("Codex 자동복구 시작", alerts[0])
                self.assertIn("Codex 자동복구 결과", alerts[1])
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 시작 chat=test\n처리 완료 chat=test duration=1s\n")
                health._process_cycle(state)
                self.assertEqual(len(alerts), 3)
                self.assertIn("재처리 성공", alerts[2])
            finally:
                for name, value in original.items():
                    setattr(health, name, value)

    def test_usage_limit_alert_never_launches_codex_repair(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.log"
            state_file = Path(td) / "state.json"
            log.write_text("기존 로그\n", encoding="utf-8")
            original = {
                "TARGETS": health.TARGETS,
                "STATE_FILE": health.STATE_FILE,
                "_service_running": health._service_running,
                "_run_codex_repair": health._run_codex_repair,
                "_send_alert": health._send_alert,
            }
            alerts = []
            repairs = []
            health.TARGETS = {"claude": {"label": "present", "log": log}}
            health.STATE_FILE = state_file
            health._service_running = lambda label: True
            health._run_codex_repair = lambda event: repairs.append(event) or "should not run"
            health._send_alert = alerts.append
            try:
                state = {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}, "repair_results": {}, "recovery_watch": {}}
                health._process_cycle(state)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("provider error rate_limit_error retry-after: 60\n")
                health._process_cycle(state)
                self.assertEqual(repairs, [])
                self.assertEqual(len(alerts), 1)
                self.assertIn("사용량 제한 감지", alerts[0])
                self.assertIn("자동 Codex 복구: 실행하지 않음", alerts[0])
            finally:
                for name, value in original.items():
                    setattr(health, name, value)

    def test_usage_recovery_is_reported_only_after_a_real_success(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.log"
            log.write_text("기존 로그\n", encoding="utf-8")
            original_targets = health.TARGETS
            original_running = health._service_running
            health.TARGETS = {"codex": {"label": "present", "log": log}}
            health._service_running = lambda label: True
            try:
                state = {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}, "repair_results": {}, "recovery_watch": {}}
                health.poll_once(state, now=100)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("provider error quota exceeded; weekly window\n")
                alerts = health.poll_once(state, now=101)
                self.assertEqual([event["code"] for event in alerts], ["usage_limited"])
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 시작 chat=test\n처리 완료 chat=test duration=1s\n")
                alerts = health.poll_once(state, now=102)
                self.assertEqual([event["code"] for event in alerts], ["usage_recovered"])
                self.assertEqual(next(iter(state["usage_watch"].values()))["status"], "completed_success")
            finally:
                health.TARGETS = original_targets
                health._service_running = original_running

    def test_usage_watch_coalesces_repeated_limits_and_expires(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.log"
            log.write_text("기존 로그\n", encoding="utf-8")
            original_targets = health.TARGETS
            original_running = health._service_running
            health.TARGETS = {"codex": {"label": "present", "log": log}}
            health._service_running = lambda label: True
            try:
                state = {"initialized": False, "offsets": {}, "pending": {}, "alerted": {}, "repair_results": {}, "recovery_watch": {}}
                health.poll_once(state, now=0)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("provider error quota exceeded; weekly window\n")
                self.assertEqual([event["code"] for event in health.poll_once(state, now=1)], ["usage_limited"])
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("provider error usage limit reached; weekly window\n")
                self.assertEqual(health.poll_once(state, now=2), [])
                self.assertEqual(len([item for item in state["usage_watch"].values() if item["status"] == "waiting_for_probe"]), 1)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 시작 chat=test\n처리 완료 chat=test duration=1s\n")
                self.assertEqual([event["code"] for event in health.poll_once(state, now=3)], ["usage_recovered"])
                self.assertEqual(state["metrics"]["classified"]["usage_limited"], 2)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("provider error quota exhausted; weekly window\n")
                health.poll_once(state, now=4)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 시작 chat=stale\n처리 완료 chat=stale duration=1s\n")
                self.assertEqual(health.poll_once(state, now=4 + health.USAGE_WATCH_TTL_SECONDS + 1), [])
                self.assertTrue(any(item["status"] == "expired" for item in state["usage_watch"].values()))
            finally:
                health.TARGETS = original_targets
                health._service_running = original_running

    def test_repair_result_only_instructs_reprocess_after_success(self):
        event = {"role": "claude", "code": "service_down"}
        failed = health._format_repair_result(event, "Codex 진단 실행 실패: TimeoutExpired")
        self.assertIn("미완료/실패", failed)
        self.assertIn("지시하지 않습니다", failed)
        self.assertNotIn("@edgeai_stk_bot", failed)
        succeeded = health._format_repair_result(event, "Codex 자동 수정·main 병합·claude 서비스 재기동 완료.")
        self.assertIn("@edgeai_stk_bot", succeeded)
        self.assertIn("다시 처리하세요", succeeded)

    def test_disabled_repair_is_reported_as_not_started(self):
        event = {"role": "claude", "code": "execution_error"}
        message = health._format_repair_result(event, "자동 복구가 비활성화되어 있습니다.")
        self.assertIn("상태: 미실행", message)
        self.assertIn("시작되지 않았", message)
        self.assertNotIn("상태: 미완료/실패", message)

    def test_task_correlated_done_cannot_consume_another_pending_request(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "codex.log"
            log.write_text("baseline\n", encoding="utf-8")
            original_targets = health.TARGETS
            original_running = health._service_running
            health.TARGETS = {"codex": {"label": "present", "log": log}}
            health._service_running = lambda label: True
            try:
                state = health._default_state()
                health.poll_once(state, now=0)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 시작 task=task-a chat=x\n")
                    handle.write("처리 시작 task=task-b chat=x\n")
                    handle.write("처리 완료 task=task-b chat=x\n")
                health.poll_once(state, now=1)
                self.assertEqual(set(state["pending"]["codex"]), {"task-a"})
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 완료 task=task-c chat=x\n")
                health.poll_once(state, now=2)
                self.assertEqual(set(state["pending"]["codex"]), {"task-a"})
                alerts = health.poll_once(state, now=health.NO_RESPONSE_SECONDS + 2)
                no_response = [item for item in alerts if item.get("code") == "no_response"]
                self.assertEqual([item["task_id"] for item in no_response], ["task-a"])
                self.assertEqual(state["incidents"][no_response[0]["fingerprint"]]["status"], "open")
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("처리 완료 task=task-a chat=x\n")
                health.poll_once(state, now=health.NO_RESPONSE_SECONDS + 3)
                self.assertEqual(state["incidents"][no_response[0]["fingerprint"]]["status"], "resolved")
            finally:
                health.TARGETS = original_targets
                health._service_running = original_running

    def test_process_cycle_does_not_announce_start_when_preflight_blocks(self):
        event = {
            "role": "claude",
            "code": "execution_error",
            "fingerprint": "blocked-test",
            "message": "[Roda 감지] 실행 오류",
            "detail": "exit=1",
        }
        state = {"repair_results": {}, "recovery_watch": {}, "pending_merges": {}, "delivery_retry": []}
        alerts = []
        with mock.patch.object(health, "_retry_pending_merges"), \
                mock.patch.object(health, "poll_once", return_value=[event]), \
                mock.patch.object(health, "_save_state"), \
                mock.patch.object(health, "_send_alert", side_effect=alerts.append), \
                mock.patch.object(health, "_repair_preflight_blocker", return_value="자동 복구가 비활성화되어 있습니다."), \
                mock.patch.object(health, "_run_codex_repair", return_value="자동 복구가 비활성화되어 있습니다."):
            health._process_cycle(state)
        self.assertEqual(len(alerts), 1)
        self.assertNotIn("자동복구 시작", alerts[0])
        self.assertIn("상태: 미실행", alerts[0])

    def test_drops_legacy_polling_stopped_retry_without_dropping_real_failures(self):
        original = health.TARGETS
        health.TARGETS = {}
        try:
            state = {
                "initialized": True,
                "offsets": {},
                "pending": {},
                "alerted": {},
                "delivery_retry": [
                    {"role": "codex", "code": "polling_stopped"},
                    {"role": "codex", "code": "empty_response"},
                ],
            }

            alerts = health.poll_once(state)

            self.assertEqual([event["code"] for event in alerts], ["empty_response"])
            self.assertEqual(state["delivery_retry"], [])
        finally:
            health.TARGETS = original

    def test_planned_restart_suppresses_service_down(self):
        with tempfile.TemporaryDirectory() as td:
            original = {
                "TARGETS": health.TARGETS,
                "MAINTENANCE_FILE": health.MAINTENANCE_FILE,
                "_service_running": health._service_running,
            }
            try:
                log = Path(td) / "x.log"
                marker = Path(td) / "maintenance.json"
                log.write_text("기존 로그\n", encoding="utf-8")
                marker.write_text('{"version": 1, "roles": {"x": {"expires_at": 200}}}', encoding="utf-8")
                health.TARGETS = {"x": {"label": "present", "log": log}}
                health.MAINTENANCE_FILE = marker
                health._service_running = lambda label: False
                state = {"initialized": True, "offsets": {"x": len("기존 로그\n".encode())}, "pending": {}, "alerted": {}}
                alerts = health.poll_once(state, now=100)
                self.assertEqual(alerts, [])
                self.assertEqual(state["service_down_since"], {})
            finally:
                for name, value in original.items():
                    setattr(health, name, value)

    def test_service_down_requires_grace_period(self):
        with tempfile.TemporaryDirectory() as td:
            original = {
                "TARGETS": health.TARGETS,
                "MAINTENANCE_FILE": health.MAINTENANCE_FILE,
                "_service_running": health._service_running,
            }
            try:
                log = Path(td) / "x.log"
                marker = Path(td) / "maintenance.json"
                log.write_text("기존 로그\n", encoding="utf-8")
                health.TARGETS = {"x": {"label": "present", "log": log}}
                health.MAINTENANCE_FILE = marker
                health._service_running = lambda label: False
                state = {"initialized": True, "offsets": {"x": len("기존 로그\n".encode())}, "pending": {}, "alerted": {}}
                self.assertEqual(health.poll_once(state, now=100), [])
                self.assertEqual(health.poll_once(state, now=100 + health.SERVICE_DOWN_GRACE_SECONDS - 1), [])
                alerts = health.poll_once(state, now=100 + health.SERVICE_DOWN_GRACE_SECONDS)
                self.assertEqual([event["code"] for event in alerts], ["service_down"])
            finally:
                for name, value in original.items():
                    setattr(health, name, value)

    def test_repair_merge_is_guarded_by_integration_lock(self):
        source = (Path(__file__).parents[1] / "bin" / "roda-telegram-health-monitor.py").read_text()
        self.assertIn("integration_lock(SOURCE_REPO)", source)
        self.assertIn('"merge", "--no-ff"', source)
        self.assertIn('"merge", "--abort"', source)

    def test_merge_abort_failure_is_reported_as_critical_and_never_restarts(self):
        commands = []

        def run(command, **_kwargs):
            commands.append(command)
            if command[-2:] == ["status", "--porcelain"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[-2:] == ["rev-parse", "HEAD"]:
                return mock.Mock(returncode=0, stdout="base-head\n", stderr="")
            if "--no-ff" in command:
                return mock.Mock(returncode=1, stdout="", stderr="conflict")
            if command[-3:] == ["-q", "--verify", "MERGE_HEAD"]:
                return mock.Mock(returncode=0, stdout="repair-head\n", stderr="")
            if command[-2:] == ["merge", "--abort"]:
                return mock.Mock(returncode=1, stdout="", stderr="abort failed")
            raise AssertionError(command)

        with mock.patch.object(health.subprocess, "run", side_effect=run), \
                mock.patch.object(health, "integration_lock", None):
            result = health._merge_repair_commit_and_restart(
                role="codex", code="execution_error", repair_commit="repair-head", fingerprint="f",
            )
        self.assertIn("CRITICAL", result)
        self.assertFalse(any(str(health.RESTART_HELPER) in command for command in commands))

    def test_retryable_merge_failure_queues_repair_commit_once_with_creation_time(self):
        event = {
            "role": "codex",
            "code": "empty_response",
            "fingerprint": "repair-fingerprint",
            "detail": "empty response",
        }
        state = {"pending_merges": {}}
        with tempfile.TemporaryDirectory() as td:
            codex_bin = Path(td) / "codex"
            codex_bin.touch()
            worktree = Path(td) / "repair" / event["fingerprint"]
            original = {
                "CODEX_BIN": health.CODEX_BIN,
                "REPAIR_ROOT": health.REPAIR_ROOT,
                "AUTO_REPAIR_APPROVAL_FILE": health.AUTO_REPAIR_APPROVAL_FILE,
                "repository_lifecycle_lock": health.repository_lifecycle_lock,
                "CODEX_DIAGNOSIS_ENABLED": health.CODEX_DIAGNOSIS_ENABLED,
                "AUTO_REPAIR_ENABLED": health.AUTO_REPAIR_ENABLED,
            }

            def run(command, **_kwargs):
                if "worktree" in command:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if "exec" in command:
                    return mock.Mock(
                        returncode=0,
                        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"fixed"}}\n',
                        stderr="",
                    )
                if command[-2:] == ["status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout=" M bin/example.py\n", stderr="")
                if "diff" in command:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if command[-2:] == ["add", "-A"] or "commit" in command:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return mock.Mock(returncode=0, stdout="repair-commit\n", stderr="")
                raise AssertionError(f"unexpected subprocess command: {command}")

            health.CODEX_BIN = codex_bin
            health.REPAIR_ROOT = Path(td) / "repair"
            health.AUTO_REPAIR_APPROVAL_FILE = Path(td) / "approvals.json"
            health.AUTO_REPAIR_APPROVAL_FILE.write_text(
                '{"approvals":{"repair-fingerprint":{"approved":true,"expires_at":1800000000}}}',
                encoding="utf-8",
            )
            health.AUTO_REPAIR_APPROVAL_FILE.chmod(0o600)
            health.repository_lifecycle_lock = None
            health.CODEX_DIAGNOSIS_ENABLED = True
            health.AUTO_REPAIR_ENABLED = True
            try:
                with mock.patch.object(health.subprocess, "run", side_effect=run), \
                        mock.patch.object(health, "_merge_repair_commit_and_restart", return_value="main 작업공간이 깨끗하지 않아 자동 병합하지 않았습니다."), \
                        mock.patch.object(health.time, "time", return_value=1700000000):
                    first = health._run_codex_repair_impl(event, state)
                    worktree.mkdir(parents=True)
                    second = health._run_codex_repair_impl(event, state)
            finally:
                for name, value in original.items():
                    setattr(health, name, value)

        self.assertIn("작업공간이 깨끗하지 않아", first)
        self.assertIn("작업공간이 깨끗하지 않아", second)
        self.assertEqual(list(state["pending_merges"]), [event["fingerprint"]])
        self.assertEqual(state["pending_merges"][event["fingerprint"]]["repair_commit"], "repair-commit")
        self.assertEqual(state["pending_merges"][event["fingerprint"]]["queued_at"], 1700000000)

    def test_automatic_repair_requires_explicit_fingerprint_approval(self):
        event = {"role": "codex", "code": "empty_response", "fingerprint": "unapproved"}
        original = (health.AUTO_REPAIR_ENABLED, health.AUTO_REPAIR_APPROVAL_FILE)
        with tempfile.TemporaryDirectory() as td:
            health.AUTO_REPAIR_ENABLED = True
            health.AUTO_REPAIR_APPROVAL_FILE = Path(td) / "approvals.json"
            health.AUTO_REPAIR_APPROVAL_FILE.write_text(
                '{"approvals":{"different":{"approved":true,"expires_at":9999999999}}}',
                encoding="utf-8",
            )
            try:
                result = health._run_codex_repair_impl(event, {})
            finally:
                health.AUTO_REPAIR_ENABLED, health.AUTO_REPAIR_APPROVAL_FILE = original
        self.assertIn("자동 복구 승인 없음", result)

    def test_expired_repair_approval_is_rejected(self):
        event = {"fingerprint": "expired"}
        original = health.AUTO_REPAIR_APPROVAL_FILE
        with tempfile.TemporaryDirectory() as td:
            health.AUTO_REPAIR_APPROVAL_FILE = Path(td) / "approvals.json"
            health.AUTO_REPAIR_APPROVAL_FILE.write_text(
                '{"approvals":{"expired":{"approved":true,"expires_at":1}}}',
                encoding="utf-8",
            )
            try:
                self.assertFalse(health._repair_approval_granted(event, now=2))
            finally:
                health.AUTO_REPAIR_APPROVAL_FILE = original

    def test_pending_merge_retry_reuses_commit_without_rediagnosis_and_orders_success_followup(self):
        fingerprint = "queued-fingerprint"
        state = {
            "pending_merges": {
                fingerprint: {
                    "role": "codex",
                    "code": "empty_response",
                    "repair_commit": "saved-commit",
                    "worktree": "/tmp/repair-worktree",
                    "summary": "diagnosis summary",
                    "queued_at": 1700000000,
                }
            },
            "repair_results": {},
            "recovery_watch": {},
        }
        sequence = []

        def merge(**kwargs):
            sequence.append(("merge", kwargs))
            return "Codex 자동 수정·main 병합·codex 서비스 재기동 완료."

        with mock.patch.object(health.time, "time", return_value=1700000001), \
                mock.patch.object(health, "_merge_repair_commit_and_restart", side_effect=merge), \
                mock.patch.object(health, "_repair_approval_granted", return_value=True), \
                mock.patch.object(health, "_run_codex_repair", side_effect=AssertionError("rediagnosis must not run")), \
                mock.patch.object(health, "_save_state", side_effect=lambda _state: sequence.append(("save", None))), \
                mock.patch.object(health, "_send_alert", side_effect=lambda message: sequence.append(("alert", message))):
            health._retry_pending_merges(state)

        self.assertEqual(state["pending_merges"], {})
        self.assertEqual(sequence[0][0], "merge")
        self.assertEqual(sequence[0][1]["repair_commit"], "saved-commit")
        self.assertEqual([item[0] for item in sequence], ["merge", "save", "alert"])
        self.assertEqual(state["recovery_watch"][fingerprint]["status"], "awaiting_reprocess")
        self.assertIn("Codex 자동 수정", state["repair_results"][fingerprint])

    def test_pending_merge_retry_failure_stays_queued_without_followup(self):
        fingerprint = "blocked-fingerprint"
        info = {
            "role": "codex",
            "code": "empty_response",
            "repair_commit": "saved-commit",
            "worktree": "/tmp/repair-worktree",
            "summary": "diagnosis summary",
            "queued_at": 1700000000,
        }
        state = {"pending_merges": {fingerprint: info}, "repair_results": {}, "recovery_watch": {}}
        with mock.patch.object(health.time, "time", return_value=1700000001), \
                mock.patch.object(health, "_merge_repair_commit_and_restart", return_value="Codex 수정은 생성됐지만 main 병합에 실패했습니다: conflict"), \
                mock.patch.object(health, "_repair_approval_granted", return_value=True), \
                mock.patch.object(health, "_save_state") as save_state, \
                mock.patch.object(health, "_send_alert") as send_alert:
            health._retry_pending_merges(state)

        self.assertEqual(state["pending_merges"][fingerprint], info)
        self.assertEqual(state["recovery_watch"], {})
        save_state.assert_not_called()
        send_alert.assert_not_called()

    def test_pending_merge_ttl_removes_item_at_exact_boundary_and_requests_manual_merge(self):
        fingerprint = "expired-fingerprint"
        state = {
            "pending_merges": {
                fingerprint: {
                    "role": "codex",
                    "code": "empty_response",
                    "repair_commit": "saved-commit",
                    "worktree": "/tmp/repair-worktree",
                    "summary": "diagnosis summary",
                    "queued_at": 1700000000,
                }
            },
            "repair_results": {},
            "recovery_watch": {},
        }
        with mock.patch.object(health.time, "time", return_value=1700000000 + health.PENDING_MERGE_TTL_SECONDS), \
                mock.patch.object(health, "_merge_repair_commit_and_restart") as merge, \
                mock.patch.object(health, "_save_state"), \
                mock.patch.object(health, "_send_alert") as send_alert:
            health._retry_pending_merges(state)

        merge.assert_not_called()
        self.assertNotIn(fingerprint, state["pending_merges"])
        self.assertIn("수동 병합이 필요합니다", state["repair_results"][fingerprint])
        send_alert.assert_called_once()
        self.assertIn("saved-commit", send_alert.call_args.args[0])

    def test_pending_merge_without_approval_never_merges(self):
        fingerprint = "approval-required"
        state = {
            "pending_merges": {
                fingerprint: {
                    "role": "codex",
                    "code": "empty_response",
                    "repair_commit": "saved-commit",
                    "worktree": "/tmp/repair-worktree",
                    "queued_at": 1700000000,
                }
            }
        }
        with mock.patch.object(health.time, "time", return_value=1700000001), \
                mock.patch.object(health, "_repair_approval_granted", return_value=False), \
                mock.patch.object(health, "_merge_repair_commit_and_restart") as merge:
            health._retry_pending_merges(state)
        merge.assert_not_called()
        self.assertIn(fingerprint, state["pending_merges"])

    def test_merge_failure_never_restarts_but_success_restarts_after_merge(self):
        original_lock = health.integration_lock
        health.integration_lock = None
        try:
            def run_dirty(command, **_kwargs):
                if command[-2:] == ["status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout=" M tracked.py\n", stderr="")
                raise AssertionError(f"dirty main should stop before: {command}")

            with mock.patch.object(health.subprocess, "run", side_effect=run_dirty) as run:
                result = health._merge_repair_commit_and_restart(
                    role="codex", code="empty_response", repair_commit="saved-commit", fingerprint="fp"
                )
            self.assertIn("작업공간이 깨끗하지 않아", result)
            self.assertEqual(run.call_count, 1)

            commands = []

            def run_conflict(command, **_kwargs):
                commands.append(command)
                if command[-2:] == ["status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return mock.Mock(returncode=0, stdout="main-head\n", stderr="")
                if "merge" in command and "--abort" not in command:
                    return mock.Mock(returncode=1, stdout="", stderr="CONFLICT")
                if "MERGE_HEAD" in command:
                    return mock.Mock(returncode=0, stdout="saved-commit\n", stderr="")
                if "--abort" in command:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected conflict command: {command}")

            with mock.patch.object(health.subprocess, "run", side_effect=run_conflict):
                result = health._merge_repair_commit_and_restart(
                    role="codex", code="empty_response", repair_commit="saved-commit", fingerprint="fp"
                )
            self.assertIn("병합에 실패했습니다", result)
            self.assertFalse(any(str(health.RESTART_HELPER) in str(command) for command in commands))

            def run_success(command, **_kwargs):
                if command[-2:] == ["status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return mock.Mock(returncode=0, stdout="main-head\n", stderr="")
                if "merge" in command:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if str(health.RESTART_HELPER) in str(command):
                    return mock.Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected success command: {command}")

            with mock.patch.object(health.subprocess, "run", side_effect=run_success) as run:
                result = health._merge_repair_commit_and_restart(
                    role="codex", code="empty_response", repair_commit="saved-commit", fingerprint="fp"
                )
            self.assertIn("서비스 재기동 완료", result)
            self.assertTrue(any(str(health.RESTART_HELPER) in str(call.args[0]) for call in run.call_args_list))
        finally:
            health.integration_lock = original_lock

    def test_merge_fails_closed_when_git_status_fails(self):
        original_lock = health.integration_lock
        health.integration_lock = None
        try:
            with mock.patch.object(
                health.subprocess,
                "run",
                return_value=mock.Mock(returncode=128, stdout="", stderr="not a repository"),
            ) as run:
                result = health._merge_repair_commit_and_restart(
                    role="codex", code="empty_response", repair_commit="saved-commit", fingerprint="fp"
                )
            self.assertIn("상태를 확인하지 못해", result)
            self.assertEqual(run.call_count, 1)
        finally:
            health.integration_lock = original_lock

    def test_main_dirty_signal_is_suppressed_for_24_hours_and_fires_at_boundary(self):
        original_dirty_lines = health._source_repo_tracked_dirty_lines
        health._source_repo_tracked_dirty_lines = lambda: [" M tracked.py"]
        try:
            base = 2 * health.MAIN_DIRTY_ALERT_INTERVAL_SECONDS + 10
            state = {}
            first = health._check_main_dirty(state, base)
            suppressed = health._check_main_dirty(state, base + health.MAIN_DIRTY_ALERT_INTERVAL_SECONDS - 1)
            boundary = health._check_main_dirty(state, base + health.MAIN_DIRTY_ALERT_INTERVAL_SECONDS)
        finally:
            health._source_repo_tracked_dirty_lines = original_dirty_lines

        self.assertEqual(first["code"], "main_dirty")
        self.assertIsNone(suppressed)
        self.assertEqual(boundary["code"], "main_dirty")


if __name__ == "__main__":
    unittest.main()
