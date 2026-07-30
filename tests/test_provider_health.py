#!/usr/bin/env python3
"""Pure tests for the provider result contract; no CLI or network calls."""

import sys
import asyncio
import ast
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "bin"))

from discord_bot_common import (
    ProviderResult,
    ProviderFallbackResult,
    clear_provider_context,
    format_provider_context,
    format_provider_fallback_failure,
    load_provider_context,
    run_provider_attempt,
    run_provider_fallback_chain,
    save_provider_context,
)  # noqa: E402


class ProviderResultTests(unittest.TestCase):
    def test_success_requires_nonempty_output(self):
        self.assertTrue(ProviderResult("agy", 0, "answer").usable)
        self.assertEqual(ProviderResult("agy", 0, "answer").status, "ok")
        self.assertEqual(ProviderResult("agy", 0, "").status, "empty")

    def test_quota_error_is_classified(self):
        result = ProviderResult("codex", 1, "rate_limit_error: hit your usage limit")
        self.assertEqual(result.status, "quota")
        self.assertFalse(result.usable)

    def test_long_answer_about_rate_limits_is_not_depletion(self):
        result = ProviderResult("agy", 0, "rate limiting explained: " + ("x" * 220))
        self.assertEqual(result.status, "ok")

    def test_non_quota_failure_is_error(self):
        self.assertEqual(ProviderResult("claude", 1, "process crashed").status, "error")

    def test_diagnostic_keeps_tail_and_handles_empty(self):
        result = ProviderResult("codex", 1, "prefix\n" + ("x" * 400))
        self.assertEqual(len(result.diagnostic(20)), 20)
        self.assertIn("응답 없음", ProviderResult("codex", 1, "").diagnostic())


class ProviderAttemptTests(unittest.TestCase):
    def test_fake_success_and_failure_are_captured(self):
        with tempfile.TemporaryDirectory() as directory:
            success = os.path.join(directory, "success.sh")
            failure = os.path.join(directory, "failure.sh")
            for path, body in (
                (success, '#!/bin/sh\nprintf "agy answer\\n"\n'),
                (failure, '#!/bin/sh\nprintf "quota exceeded\\n"\nexit 1\n'),
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(body)
                os.chmod(path, 0o700)

            async def run():
                return await asyncio.gather(
                    run_provider_attempt("agy", [success], 2),
                    run_provider_attempt("codex", [failure], 2),
                )

            good, bad = asyncio.run(run())
            self.assertEqual((good.status, good.output), ("ok", "agy answer"))
            self.assertEqual(bad.status, "quota")

    def test_timeout_returns_without_waiting_for_child(self):
        with tempfile.TemporaryDirectory() as directory:
            sleeper = os.path.join(directory, "sleeper.sh")
            with open(sleeper, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\nsleep 30\n")
            os.chmod(sleeper, 0o700)
            result = asyncio.run(run_provider_attempt("agy", [sleeper], 0.1))
            self.assertEqual(result.status, "error")
            self.assertIn("timeout after", result.output)

    def test_spawn_failure_is_a_result(self):
        result = asyncio.run(run_provider_attempt("agy", ["/does/not/exist"], 2))
        self.assertEqual(result.status, "error")
        self.assertIn("spawn failed", result.output)

    def test_process_lifecycle_callbacks_bracket_the_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            script = os.path.join(directory, "success.sh")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write('#!/bin/sh\nprintf "ok\\n"\n')
            os.chmod(script, 0o700)
            events = []
            result = asyncio.run(run_provider_attempt(
                "agy",
                [script],
                2,
                on_process_started=lambda proc: events.append(("started", proc.pid)),
                on_process_finished=lambda proc: events.append(("finished", proc.pid)),
            ))
            self.assertEqual(result.status, "ok")
            self.assertEqual([event[0] for event in events], ["started", "finished"])
            self.assertEqual(events[0][1], events[1][1])


class ProviderFallbackChainTests(unittest.TestCase):
    def test_successful_antigravity_does_not_consume_codex(self):
        calls = []

        async def agy():
            calls.append("agy")
            return ProviderResult("agy", 0, "answer")

        async def gate():
            calls.append("gate")
            return None

        async def codex():
            calls.append("codex")
            return ProviderResult("codex", 0, "fallback")

        result = asyncio.run(run_provider_fallback_chain(agy, gate, codex))
        self.assertEqual(calls, ["agy"])
        self.assertIsNone(result.codex)

    def test_failed_antigravity_then_codex(self):
        calls = []

        async def agy():
            calls.append("agy")
            return ProviderResult("agy", 1, "quota exceeded")

        async def gate():
            calls.append("gate")
            return None

        async def codex():
            calls.append("codex")
            return ProviderResult("codex", 0, "answer")

        result = asyncio.run(run_provider_fallback_chain(agy, gate, codex))
        self.assertEqual(calls, ["agy", "gate", "codex"])
        self.assertEqual(result.codex.status, "ok")

    def test_failed_antigravity_and_codex_gate_stops_before_codex(self):
        calls = []

        async def agy():
            calls.append("agy")
            return ProviderResult("agy", 1, "process crashed")

        async def gate():
            calls.append("gate")
            return "codex 7d window is low"

        async def codex():
            calls.append("codex")
            return ProviderResult("codex", 0, "must not run")

        result = asyncio.run(run_provider_fallback_chain(agy, gate, codex))
        self.assertEqual(calls, ["agy", "gate"])
        self.assertEqual(result.codex_skip_reason, "codex 7d window is low")

    def test_stop_predicate_stops_before_codex_after_antigravity(self):
        calls = []
        should_continue = True

        async def agy():
            calls.append("agy")
            return ProviderResult("agy", 1, "process crashed")

        async def gate():
            calls.append("gate")
            return None

        async def codex():
            calls.append("codex")
            return ProviderResult("codex", 0, "must not run")

        def is_running():
            return should_continue

        should_continue = False
        result = asyncio.run(run_provider_fallback_chain(agy, gate, codex, is_running))
        self.assertEqual(calls, ["agy"])
        self.assertEqual(result.stop_reason, "사용자가 중단함")

    def test_failure_envelope_keeps_both_provider_labels_and_discord_limit(self):
        result = ProviderFallbackResult(
            ProviderResult("agy", 1, "a" * 900),
            ProviderResult("codex", 1, "c" * 900),
            None,
        )
        envelope = format_provider_fallback_failure(result)
        self.assertLessEqual(len(envelope), 1900)
        self.assertIn("Antigravity:", envelope)
        self.assertIn("Codex:", envelope)


class ProviderContextTests(unittest.TestCase):
    def test_context_is_bounded_and_round_trips_as_untrusted_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fallback.json"
            save_provider_context(path, "codex", "u" * 5000, "r" * 7000)
            data = load_provider_context(path)
            self.assertEqual(len(data["user_message"]), 4000)
            self.assertEqual(len(data["response"]), 6000)
            rendered = format_provider_context(data)
            self.assertIn("참고", rendered)
            self.assertIn("provider: codex", rendered)
            clear_provider_context(path)
            self.assertIsNone(load_provider_context(path))


class DiscordIntegrationSourceTests(unittest.TestCase):
    SOURCE = Path(__file__).parents[1] / "bin" / "discord-bot.py"

    def test_nested_claude_runner_updates_global_stop_handle(self):
        tree = ast.parse(self.SOURCE.read_text())
        runners = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_once"
        ]
        self.assertEqual(len(runners), 1)
        globals_in_runner = {
            name
            for statement in runners[0].body
            if isinstance(statement, ast.Global)
            for name in statement.names
        }
        self.assertIn("FREE_CHAT_CURRENT_PROC", globals_in_runner)

    def test_fallback_cli_is_read_only(self):
        source = self.SOURCE.read_text()
        self.assertIn('"--mode", "plan"', source)
        self.assertIn('"-s", "read-only"', source)


if __name__ == "__main__":
    unittest.main()
