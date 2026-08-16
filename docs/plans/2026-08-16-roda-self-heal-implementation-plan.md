# Roda Self-Healing Auto-Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: dispatch each task to a fresh subagent (this environment's Agent tool, or the Workflow tool for a deterministic multi-task pipeline — recommended) or use the executing-plans skill to work through this plan task-by-task in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This repo requires the verify-task gate for all edits.** The main session cannot `Edit`/`Write` `bin/*.py` or `tests/*.py` directly. Save each task's diff intent to a task file and run `python3 bin/verify-task-orchestrator.py --task-file <file> --cwd <repo> --session-id <id>` (Codex implements, Claude/Antigravity review) before any change lands. See `docs/specs/2026-08-16-roda-self-heal-expansion-design.md`.

**Goal:** Turn the existing Codex-only, approval-gated auto-repair (`_run_codex_repair_impl`) into a fully-automatic, multi-implementer self-healing pipeline — Codex→Claude→Antigravity fallback, bounded by a 5-minute wall-clock budget, guarded by three circuit breakers, and handing off cleanly to the already-built escalation chain (commits `ebbb0d2`..`3f9fc84`) when it fails.

**Architecture:** All new logic lives in `bin/roda-telegram-health-monitor.py`, on top of the existing incident ledger and the existing `_merge_repair_commit_and_restart`/`integration_lock`/`_atomic_write` machinery. A new `_attempt_self_heal(event, state)` entry point replaces the Codex-only branch inside `_process_cycle`; on any failure it hands off into the existing `escalation_stage="awaiting_ack"` flow (already built by Tasks 1-7 of the escalation plan) by setting `routed_at=now()`. `_run_implementer_cli` is the single dispatch point for all three implementers, returning a structured result dict so every caller (success-definition check, audit log) reads the same shape regardless of which robot ran.

**Tech Stack:** Python 3 stdlib only (`subprocess`, `re`, `json`, `time`), `unittest` + `unittest.mock`, no new dependencies. Reuses `codex exec`, `agy --print --mode accept-edits`, and (newly) `claude -p --output-format json` (exact flags already used by `bin/verify-task-orchestrator.py:431`).

## Global Constraints

- Every new constant/field/function in this plan is exact — no `TBD`, no bare `pass`, no "add validation here".
- `NON_REPAIRABLE_CODES` (`auth_error`, `session_limited`, `rate_limited`... — actually see §5 split below) stays the entry point for "skip self-heal entirely, go straight to escalation" — do not change its existing members; only add a *separate* dynamic-exclusion layer on top (design doc §8).
- `main_dirty` never gets a self-heal attempt — it already routes straight to the escalation chain via `NON_REPAIRABLE_CODES` membership; do not change this.
- Per-implementer CLI timeout stays 180s (existing convention, `_run_codex_repair_impl:1004`); the *whole* self-heal attempt (all 3 implementers combined) gets a separate, new 5-minute (300s) wall-clock budget (design doc §1/§2, per round-2 Antigravity gap #1).
- Merge requires the full test suite (`python3 -m unittest discover -s tests -p "test_*.py"`) to pass 100%, with **no exception** — this applies to both the default 2-reviewer track and the low-risk 1-reviewer track (design doc §3/§5, round-2 Antigravity gap #2).
- Protected files (design doc §4) are always 2-reviewer, never eligible for the 1-reviewer low-risk exception: anything under `.github/workflows/`, `requirements.txt`, `package.json`, `Dockerfile`, `go.mod`, `Gemfile`, `pyproject.toml`, any path containing `secret`, `token`, `credential`, `.pem`, `.key`, `launchd`, or the orchestrator itself (`bin/verify-task-orchestrator.py`), or any line inside `bin/roda-telegram-health-monitor.py` between `_default_state`/`_migrate_state`/`STATE_SCHEMA_VERSION` (state-schema migration code).
- Low-risk exception (1 reviewer instead of 2) requires ALL of: no protected file touched, ≤30 changed lines total, ≤3 files changed, full test suite passes (design doc §5).
- Circuit breakers (design doc §6) use the existing `_atomic_write`/`integration_lock` pattern for their counters — no new locking primitive.
- Revert-on-recurrence (design doc §7): on conflict, `git revert --abort` and escalate at top severity — never attempt automatic conflict resolution.
- All new persistent state lives in `telegram-health-monitor.json` via a schema bump `STATE_SCHEMA_VERSION` 6→7 (this repo already bumped 5→6 for the escalation plan; follow that same migration pattern in `_migrate_state`).

---

## File Structure

- **Modify `bin/roda-telegram-health-monitor.py`** (all new logic, ~2011 lines currently):
  - New constant `CLAUDE_BIN` near `AGY_BIN` (line 60) — same env-var-with-default pattern.
  - New constants: `PROTECTED_FILE_PATTERNS`, `DYNAMIC_EXCLUDE_CODES`, `SELF_HEAL_TOTAL_TIMEOUT_SECONDS` (300), `SELF_HEAL_FINGERPRINT_ATTEMPT_LIMIT` (2), `SELF_HEAL_FINGERPRINT_WINDOW_SECONDS` (86400), `SELF_HEAL_GLOBAL_MERGE_LIMIT` (3), `SELF_HEAL_GLOBAL_MERGE_WINDOW_SECONDS` (86400), `SELF_HEAL_RECURRENCE_WINDOW_SECONDS` (3600), `SELF_HEAL_LOW_RISK_MAX_LINES` (30), `SELF_HEAL_LOW_RISK_MAX_FILES` (3).
  - `_default_state()`/`_migrate_state()` — bump `STATE_SCHEMA_VERSION` 6→7, add `"self_heal_attempts": {}`, `"self_heal_merges": []`, `"self_heal_manual_mode": {"active": False, "since": None}`, `"self_heal_watch": {}`, `"self_heal_blacklist": {}`.
  - `_strip_diff_fences(text) -> str`, `_run_implementer_cli(role, prompt, worktree) -> dict` — Task 1.
  - `_diff_touches_protected_files(changed_files) -> bool`, `_is_low_risk_diff(changed_files, diff_text) -> bool`, `_run_full_test_suite() -> bool`, `_merge_allowed(review_count, low_risk, tests_passed) -> bool` — Task 2.
  - `_check_fingerprint_attempt_budget`, `_record_self_heal_attempt`, `_check_global_merge_budget`, `_record_self_heal_merge`, `_enter_manual_mode`, `_manual_mode_active` — Task 3.
  - `_watch_self_heal_merge`, `_check_self_heal_recurrence`, `_revert_self_heal_commit`, `_blacklist_fingerprint`, `_is_blacklisted` — Task 4.
  - `_implementer_chain(state, current) -> list[str]` — Task 5.
  - `_attempt_self_heal(event, state) -> bool`, plus the `_process_cycle` call-site rewrite — Task 6.
  - `_record_self_heal_audit` (folded into `_attempt_self_heal` in Task 6, formalized/tested in Task 7).
- **Test: `tests/test_roda_telegram_health_monitor.py`** — new tests for every function above, following the file's existing `mock.patch.object(health, ...)` conventions.

---

### Task 1: Implementer execution contract (`_run_implementer_cli`) + Claude wrapper

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py:60` (add `CLAUDE_BIN` near `AGY_BIN`), and add new functions just above `def poll_once` (currently line 1762).
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: `AGY_BIN` (existing, line 60), `CODEX_BIN` (existing, line ~62).
- Produces: `_strip_diff_fences(text: str) -> str`; `_run_implementer_cli(role: str, prompt: str, worktree: Path, *, timeout: int = 180) -> dict` returning
  `{"status": "success"|"no_change"|"apply_failed"|"timeout"|"provider_error", "diff": str|None, "changed_files": list[str], "exit_code": int|None, "timed_out": bool, "stderr_tail": str}`.
  `diff` is populated for EVERY successful role (not just `claude`) — codex/antigravity modify the
  worktree directly, so their diff text is captured via `git diff` after the run, giving `_is_low_risk_diff`
  (Task 2) a diff to measure regardless of which implementer produced the fix. `timeout` lets the
  caller (Task 6) shrink the per-attempt budget to whatever remains of the overall 5-minute cap.
  Every later task (2, 6, 7) calls `_run_implementer_cli` and reads this exact dict shape.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_roda_telegram_health_monitor.py`:

```python
    def test_strip_diff_fences_removes_markdown_wrapper(self):
        text = "여기 패치입니다:\n```diff\ndiff --git a/x.py b/x.py\n+print(1)\n```\n"
        self.assertEqual(
            health._strip_diff_fences(text),
            "diff --git a/x.py b/x.py\n+print(1)",
        )

    def test_strip_diff_fences_passes_through_bare_diff(self):
        text = "diff --git a/x.py b/x.py\n+print(1)"
        self.assertEqual(health._strip_diff_fences(text), text)

    def test_run_implementer_cli_codex_success(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            def run(command, **kwargs):
                if command[0] == str(health.CODEX_BIN):
                    return mock.Mock(returncode=0, stdout='{"type":"item.completed","item":{"type":"agent_message","text":"fixed it"}}\n', stderr="")
                if command[-2:] == ["status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout=" M x.py\n", stderr="")
                if command[-1:] == ["HEAD"] and "diff" in command:
                    return mock.Mock(returncode=0, stdout="diff --git a/x.py b/x.py\n+print(1)\n", stderr="")
                raise AssertionError(command)
            with mock.patch.object(health.subprocess, "run", side_effect=run):
                result = health._run_implementer_cli("codex", "fix it", worktree)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["changed_files"], ["x.py"])
        self.assertFalse(result["timed_out"])
        self.assertIn("diff --git", result["diff"])

    def test_run_implementer_cli_respects_custom_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            calls = []
            def run(command, **kwargs):
                calls.append(kwargs.get("timeout"))
                if command[0] == str(health.CODEX_BIN):
                    return mock.Mock(returncode=0, stdout="", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(health.subprocess, "run", side_effect=run):
                health._run_implementer_cli("codex", "fix it", worktree, timeout=45)
        self.assertEqual(calls[0], 45)

    def test_run_implementer_cli_codex_no_change(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            def run(command, **kwargs):
                if command[0] == str(health.CODEX_BIN):
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if command[-2:] == ["status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(command)
            with mock.patch.object(health.subprocess, "run", side_effect=run):
                result = health._run_implementer_cli("codex", "fix it", worktree)
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["changed_files"], [])

    def test_run_implementer_cli_codex_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            with mock.patch.object(health.subprocess, "run", side_effect=health.subprocess.TimeoutExpired(cmd="codex", timeout=180)):
                result = health._run_implementer_cli("codex", "fix it", worktree)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["timed_out"])

    def test_run_implementer_cli_antigravity_success(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            def run(command, **kwargs):
                if command[0] == str(health.AGY_BIN):
                    self.assertIn("--mode", command)
                    self.assertIn("accept-edits", command)
                    self.assertNotIn("plan", command)
                    return mock.Mock(returncode=0, stdout="done", stderr="")
                if command[-2:] == ["status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout=" M y.py\n", stderr="")
                raise AssertionError(command)
            with mock.patch.object(health.subprocess, "run", side_effect=run):
                result = health._run_implementer_cli("antigravity", "fix it", worktree)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["changed_files"], ["y.py"])

    def test_run_implementer_cli_claude_success_strips_fences_and_applies(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            diff_text = "diff --git a/z.py b/z.py\n--- a/z.py\n+++ b/z.py\n@@ -1 +1 @@\n-old\n+new\n"
            claude_json = json.dumps({"result": f"```diff\n{diff_text}```"})
            def run(command, **kwargs):
                if command[0] == str(health.CLAUDE_BIN):
                    self.assertIn("--output-format", command)
                    self.assertIn("json", command)
                    return mock.Mock(returncode=0, stdout=claude_json, stderr="")
                if command[-2:] == ["apply", "--check"] or command[-1] == "z.diff":
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if "apply" in command:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if command[-2:] == ["status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout=" M z.py\n", stderr="")
                raise AssertionError(command)
            with mock.patch.object(health.subprocess, "run", side_effect=run):
                result = health._run_implementer_cli("claude", "fix it", worktree)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["changed_files"], ["z.py"])

    def test_run_implementer_cli_claude_apply_check_failure_is_apply_failed(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            claude_json = json.dumps({"result": "diff --git a/z.py b/z.py\nnot a valid patch"})
            def run(command, **kwargs):
                if command[0] == str(health.CLAUDE_BIN):
                    return mock.Mock(returncode=0, stdout=claude_json, stderr="")
                if "apply" in command and "--check" in command:
                    return mock.Mock(returncode=1, stdout="", stderr="corrupt patch")
                raise AssertionError(command)
            with mock.patch.object(health.subprocess, "run", side_effect=run):
                result = health._run_implementer_cli("claude", "fix it", worktree)
        self.assertEqual(result["status"], "apply_failed")

    def test_run_implementer_cli_provider_error_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            with mock.patch.object(health.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="boom")):
                result = health._run_implementer_cli("codex", "fix it", worktree)
        self.assertEqual(result["status"], "provider_error")
        self.assertIn("boom", result["stderr_tail"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k run_implementer_cli -k strip_diff_fences -v`
Expected: FAIL — `AttributeError: module 'health' has no attribute '_run_implementer_cli'`.

- [ ] **Step 3: Implement**

Near line 60 (with `AGY_BIN`), add:

```python
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
```

Above `def poll_once`, add:

```python
_DIFF_FENCE_RE = re.compile(r"```(?:diff|patch)?\n(.*?)```", re.DOTALL)


def _strip_diff_fences(text: str) -> str:
    match = _DIFF_FENCE_RE.search(text or "")
    if match:
        return match.group(1).strip()
    return (text or "").strip()


def _changed_files_in_worktree(worktree: Path) -> list[str]:
    status = subprocess.run(
        ["/usr/bin/git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    return [line[3:] for line in status.stdout.splitlines() if len(line) >= 4]


def _run_implementer_cli(role: str, prompt: str, worktree: Path, *, timeout: int = 180) -> dict:
    result_template = {
        "status": "provider_error", "diff": None, "changed_files": [],
        "exit_code": None, "timed_out": False, "stderr_tail": "",
    }
    try:
        if role == "codex":
            proc = subprocess.run(
                [str(CODEX_BIN), "exec", "--json", "-s", "workspace-write", "--skip-git-repo-check", "-C", str(worktree), "--", prompt],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        elif role == "antigravity":
            proc = subprocess.run(
                [str(AGY_BIN), "--print", "--mode", "accept-edits", prompt],
                capture_output=True, text=True, timeout=timeout, check=False, cwd=str(worktree),
            )
        elif role == "claude":
            proc = subprocess.run(
                [str(CLAUDE_BIN), "-p", "--model", "sonnet", "--effort", "medium", "--output-format", "json", prompt],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        else:
            raise ValueError(f"unknown implementer role: {role}")
    except subprocess.TimeoutExpired:
        result_template["status"] = "timeout"
        result_template["timed_out"] = True
        return result_template
    except OSError as exc:
        result_template["stderr_tail"] = f"{type(exc).__name__}: {exc}"[-500:]
        return result_template

    if proc.returncode != 0:
        result_template["status"] = "provider_error"
        result_template["exit_code"] = proc.returncode
        result_template["stderr_tail"] = (proc.stderr or "")[-500:]
        return result_template

    if role == "claude":
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            result_template["status"] = "provider_error"
            result_template["stderr_tail"] = "claude output was not valid JSON"[-500:]
            return result_template
        diff_text = _strip_diff_fences(str(payload.get("result") or ""))
        if not diff_text:
            result_template["status"] = "no_change"
            result_template["exit_code"] = proc.returncode
            return result_template
        patch_file = worktree / ".self-heal-patch.diff"
        patch_file.write_text(diff_text, encoding="utf-8")
        check = subprocess.run(
            ["/usr/bin/git", "-C", str(worktree), "apply", "--check", str(patch_file)],
            capture_output=True, text=True, check=False,
        )
        if check.returncode != 0:
            patch_file.unlink(missing_ok=True)
            result_template["status"] = "apply_failed"
            result_template["stderr_tail"] = (check.stderr or "")[-500:]
            return result_template
        apply_result = subprocess.run(
            ["/usr/bin/git", "-C", str(worktree), "apply", str(patch_file)],
            capture_output=True, text=True, check=False,
        )
        patch_file.unlink(missing_ok=True)
        if apply_result.returncode != 0:
            result_template["status"] = "apply_failed"
            result_template["stderr_tail"] = (apply_result.stderr or "")[-500:]
            return result_template
        result_template["diff"] = diff_text

    changed_files = _changed_files_in_worktree(worktree)
    result_template["changed_files"] = changed_files
    result_template["exit_code"] = proc.returncode
    if not changed_files:
        result_template["status"] = "no_change"
        return result_template
    if role != "claude":
        # codex/antigravity edit the worktree directly rather than emitting a
        # diff blob — capture one after the fact so _is_low_risk_diff (Task 2)
        # can measure the change size for these implementers too, not just claude.
        # `git add -A` first: a plain `git diff HEAD` shows nothing for brand-new
        # (untracked) files, which would let a new-file change slip through as an
        # empty diff and wrongly qualify for the low-risk 1-reviewer track.
        subprocess.run(["/usr/bin/git", "-C", str(worktree), "add", "-A"], capture_output=True, text=True, check=False)
        diff_result = subprocess.run(
            ["/usr/bin/git", "-C", str(worktree), "diff", "--cached", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        result_template["diff"] = diff_result.stdout
    result_template["status"] = "success"
    return result_template
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k run_implementer_cli -k strip_diff_fences -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: unify implementer CLI contract, add Claude patch-apply wrapper"
```

---

### Task 2: Success definition — protected files, low-risk diff, test gate, merge condition

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — new constants near the Task 1 constants, new functions above `def poll_once`.
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: nothing new from Task 1 directly (these are independent pure functions operating on `changed_files`/`diff` already produced by `_run_implementer_cli`).
- Produces: `PROTECTED_FILE_PATTERNS: frozenset[str]`; `_diff_touches_protected_files(changed_files: list[str]) -> bool`; `_is_low_risk_diff(changed_files: list[str], diff_text: str | None) -> bool`; `_run_full_test_suite() -> bool`; `_merge_allowed(review_count: int, low_risk: bool, tests_passed: bool) -> bool`. Task 6 calls all four in sequence to decide whether to merge.

- [ ] **Step 1: Write the failing tests**

```python
    def test_protected_files_block_low_risk_exception(self):
        self.assertTrue(health._diff_touches_protected_files(["requirements.txt"]))
        self.assertTrue(health._diff_touches_protected_files([".github/workflows/ci.yml"]))
        self.assertTrue(health._diff_touches_protected_files(["bin/verify-task-orchestrator.py"]))
        self.assertTrue(health._diff_touches_protected_files(["config/secrets/token.json"]))
        self.assertTrue(health._diff_touches_protected_files(["deploy/launchd/com.macagent.plist"]))
        self.assertFalse(health._diff_touches_protected_files(["bin/some-unrelated-helper.py"]))

    def test_is_low_risk_diff_requires_small_size_and_no_protected_files(self):
        small_diff = "\n".join(f"+line{i}" for i in range(10))
        self.assertTrue(health._is_low_risk_diff(["bin/x.py", "tests/test_x.py"], small_diff))
        big_diff = "\n".join(f"+line{i}" for i in range(40))
        self.assertFalse(health._is_low_risk_diff(["bin/x.py"], big_diff))
        self.assertFalse(health._is_low_risk_diff(["requirements.txt"], "+one line"))
        self.assertFalse(health._is_low_risk_diff(["a.py", "b.py", "c.py", "d.py"], "+one line"))

    def test_run_full_test_suite_reports_pass_and_fail(self):
        with mock.patch.object(health.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertTrue(health._run_full_test_suite())
        with mock.patch.object(health.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(health._run_full_test_suite())

    def test_merge_allowed_requires_tests_passed_regardless_of_track(self):
        self.assertFalse(health._merge_allowed(review_count=2, low_risk=False, tests_passed=False))
        self.assertFalse(health._merge_allowed(review_count=1, low_risk=True, tests_passed=False))

    def test_merge_allowed_default_track_needs_two_reviewers(self):
        self.assertFalse(health._merge_allowed(review_count=1, low_risk=False, tests_passed=True))
        self.assertTrue(health._merge_allowed(review_count=2, low_risk=False, tests_passed=True))

    def test_merge_allowed_low_risk_track_permits_one_reviewer(self):
        self.assertTrue(health._merge_allowed(review_count=1, low_risk=True, tests_passed=True))
        self.assertFalse(health._merge_allowed(review_count=0, low_risk=True, tests_passed=True))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k protected_files -k low_risk_diff -k full_test_suite -k merge_allowed -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

Near the Task 1 constants:

```python
PROTECTED_FILE_PATTERNS = frozenset({
    "requirements.txt", "package.json", "Dockerfile", "go.mod", "Gemfile", "pyproject.toml",
})
PROTECTED_PATH_SUBSTRINGS = (
    ".github/workflows/", "secret", "token", "credential", ".pem", ".key", "launchd",
    "verify-task-orchestrator.py",
)
SELF_HEAL_LOW_RISK_MAX_LINES = 30
SELF_HEAL_LOW_RISK_MAX_FILES = 3
```

Above `def poll_once`, alongside Task 1's functions:

```python
def _diff_touches_protected_files(changed_files: list[str]) -> bool:
    for path in changed_files:
        name = path.rsplit("/", 1)[-1]
        if name in PROTECTED_FILE_PATTERNS:
            return True
        lowered = path.lower()
        if any(substring in lowered for substring in PROTECTED_PATH_SUBSTRINGS):
            return True
    return False


def _is_low_risk_diff(changed_files: list[str], diff_text: str | None) -> bool:
    if _diff_touches_protected_files(changed_files):
        return False
    if len(changed_files) > SELF_HEAL_LOW_RISK_MAX_FILES:
        return False
    if diff_text is None:
        return False
    changed_lines = sum(1 for line in diff_text.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    return changed_lines <= SELF_HEAL_LOW_RISK_MAX_LINES


def _run_full_test_suite() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        capture_output=True, text=True, cwd=str(SOURCE_REPO), check=False,
    )
    return result.returncode == 0


def _merge_allowed(review_count: int, low_risk: bool, tests_passed: bool) -> bool:
    if not tests_passed:
        return False
    if review_count >= 2:
        return True
    return review_count >= 1 and low_risk
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k protected_files -k low_risk_diff -k full_test_suite -k merge_allowed -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: add self-heal success gate (protected files, low-risk diff, test-gated merge)"
```

---

### Task 3: State schema v7 + three circuit breakers (fingerprint budget, global budget, manual mode)

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py:47` (`STATE_SCHEMA_VERSION`), `_default_state()` (line 251), `_migrate_state()` (line 284), new constants, new functions above `def poll_once`.
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: `_atomic_write`, `integration_lock` (existing).
- Produces: `_check_fingerprint_attempt_budget(state, fingerprint, current) -> bool`; `_record_self_heal_attempt(state, fingerprint, current) -> None`; `_check_global_merge_budget(state, current) -> bool`; `_record_self_heal_merge(state, current) -> None`; `_enter_manual_mode(state, current) -> None`; `_manual_mode_active(state) -> bool`. Task 6 calls all of these to gate `_attempt_self_heal`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_migration_adds_self_heal_state_fields_and_bumps_schema_v7(self):
        state = health._migrate_state({"schema_version": 6})
        self.assertEqual(state["schema_version"], 7)
        self.assertEqual(state["self_heal_attempts"], {})
        self.assertEqual(state["self_heal_merges"], [])
        self.assertEqual(state["self_heal_manual_mode"], {"active": False, "since": None})
        self.assertEqual(state["self_heal_watch"], {})
        self.assertEqual(state["self_heal_blacklist"], {})

    def test_fingerprint_attempt_budget_allows_two_then_blocks(self):
        state = {"self_heal_attempts": {}}
        self.assertTrue(health._check_fingerprint_attempt_budget(state, "fp1", current=1000.0))
        health._record_self_heal_attempt(state, "fp1", current=1000.0)
        self.assertTrue(health._check_fingerprint_attempt_budget(state, "fp1", current=1001.0))
        health._record_self_heal_attempt(state, "fp1", current=1001.0)
        self.assertFalse(health._check_fingerprint_attempt_budget(state, "fp1", current=1002.0))

    def test_fingerprint_attempt_budget_resets_after_24h_window(self):
        state = {"self_heal_attempts": {"fp1": [1000.0, 1001.0]}}
        self.assertFalse(health._check_fingerprint_attempt_budget(state, "fp1", current=1002.0))
        self.assertTrue(health._check_fingerprint_attempt_budget(state, "fp1", current=1000.0 + 86400 + 1))

    def test_global_merge_budget_allows_three_then_blocks(self):
        state = {"self_heal_merges": []}
        for _ in range(3):
            self.assertTrue(health._check_global_merge_budget(state, current=1000.0))
            health._record_self_heal_merge(state, current=1000.0)
        self.assertFalse(health._check_global_merge_budget(state, current=1000.0))

    def test_manual_mode_enters_and_stays_active_until_explicit_clear(self):
        state = {"self_heal_manual_mode": {"active": False, "since": None}}
        self.assertFalse(health._manual_mode_active(state))
        health._enter_manual_mode(state, current=5000.0)
        self.assertTrue(health._manual_mode_active(state))
        self.assertEqual(state["self_heal_manual_mode"]["since"], 5000.0)
        # No auto-expiry: still active far in the future.
        self.assertTrue(health._manual_mode_active(state))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k self_heal_state -k fingerprint_attempt_budget -k global_merge_budget -k manual_mode -v`
Expected: FAIL — `KeyError`/`AttributeError`.

- [ ] **Step 3: Implement**

Change line 47:

```python
STATE_SCHEMA_VERSION = 7
```

In `_default_state()`, add next to `"deliberation_history": []`:

```python
        "deliberation_history": [],
        "self_heal_attempts": {},
        "self_heal_merges": [],
        "self_heal_manual_mode": {"active": False, "since": None},
        "self_heal_watch": {},
        "self_heal_blacklist": {},
```

In `_migrate_state()`, right before `_coalesce_specific_incidents(state)` (end of function), add:

```python
    state.setdefault("self_heal_attempts", {})
    state.setdefault("self_heal_merges", [])
    state.setdefault("self_heal_manual_mode", {"active": False, "since": None})
    state.setdefault("self_heal_watch", {})
    state.setdefault("self_heal_blacklist", {})
```

Above `def poll_once`, add:

```python
def _check_fingerprint_attempt_budget(state: dict, fingerprint: str, current: float) -> bool:
    attempts = state.setdefault("self_heal_attempts", {}).get(fingerprint, [])
    recent = [t for t in attempts if current - float(t) <= SELF_HEAL_FINGERPRINT_WINDOW_SECONDS]
    return len(recent) < SELF_HEAL_FINGERPRINT_ATTEMPT_LIMIT


def _record_self_heal_attempt(state: dict, fingerprint: str, current: float) -> None:
    attempts = state.setdefault("self_heal_attempts", {}).setdefault(fingerprint, [])
    attempts.append(current)
    state["self_heal_attempts"][fingerprint] = [
        t for t in attempts if current - float(t) <= SELF_HEAL_FINGERPRINT_WINDOW_SECONDS
    ]


def _check_global_merge_budget(state: dict, current: float) -> bool:
    merges = state.setdefault("self_heal_merges", [])
    recent = [t for t in merges if current - float(t) <= SELF_HEAL_GLOBAL_MERGE_WINDOW_SECONDS]
    return len(recent) < SELF_HEAL_GLOBAL_MERGE_LIMIT


def _record_self_heal_merge(state: dict, current: float) -> None:
    merges = state.setdefault("self_heal_merges", [])
    merges.append(current)
    state["self_heal_merges"] = [
        t for t in merges if current - float(t) <= SELF_HEAL_GLOBAL_MERGE_WINDOW_SECONDS
    ]


def _enter_manual_mode(state: dict, current: float) -> None:
    state["self_heal_manual_mode"] = {"active": True, "since": current}


def _manual_mode_active(state: dict) -> bool:
    return bool(state.get("self_heal_manual_mode", {}).get("active"))
```

Near the other Task 2/3 constants:

```python
SELF_HEAL_FINGERPRINT_ATTEMPT_LIMIT = 2
SELF_HEAL_FINGERPRINT_WINDOW_SECONDS = 86400
SELF_HEAL_GLOBAL_MERGE_LIMIT = 3
SELF_HEAL_GLOBAL_MERGE_WINDOW_SECONDS = 86400
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k self_heal_state -k fingerprint_attempt_budget -k global_merge_budget -k manual_mode -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS (schema bump to 7 must not break any existing schema-version assertion — check `grep -n "schema_version.*6" tests/test_roda_telegram_health_monitor.py` and update any hardcoded `== 6` assertion introduced by the escalation plan to `== 7` if migration is re-run on already-v6 state in that test).

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: add self-heal circuit breakers (fingerprint/global budgets, manual mode) — schema v7"
```

---

### Task 4: Post-merge recurrence detection — revert + blacklist

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — new constant, new functions above `def poll_once`, one new hook inside `poll_once`'s per-line classify loop (near where `_route_incident_event`/alerts get built, around the existing `code = classify_line(...)` handling — see Task 6 for the actual wiring, since this hook is exercised end-to-end there).
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks directly (works on `state["self_heal_watch"]`, a dict Task 3's schema migration already created).
- Produces: `_watch_self_heal_merge(state, fingerprint, role, code, merge_commit, current) -> None`; `_check_self_heal_recurrence(state, role, code, current) -> str | None` (returns the matching fingerprint under watch, or `None`); `_revert_self_heal_commit(commit: str) -> dict` returning `{"status": "reverted"|"conflict"|"error", "detail": str}`; `_blacklist_fingerprint(state, fingerprint, reason, current) -> None`; `_is_blacklisted(state, fingerprint) -> bool`. Task 6 wires these into the merge success path and the alert-classification path.

- [ ] **Step 1: Write the failing tests**

```python
    def test_watch_self_heal_merge_records_watch_entry(self):
        state = {"self_heal_watch": {}}
        health._watch_self_heal_merge(state, "fp1", "codex", "execution_error", "abc123", current=1000.0)
        watch = state["self_heal_watch"]["fp1"]
        self.assertEqual(watch["role"], "codex")
        self.assertEqual(watch["code"], "execution_error")
        self.assertEqual(watch["merge_commit"], "abc123")
        self.assertEqual(watch["deadline"], 1000.0 + health.SELF_HEAL_RECURRENCE_WINDOW_SECONDS)

    def test_check_self_heal_recurrence_matches_role_and_code_within_window(self):
        state = {"self_heal_watch": {
            "fp1": {"role": "codex", "code": "execution_error", "merge_commit": "abc123", "watched_at": 1000.0, "deadline": 1000.0 + 3600},
        }}
        self.assertEqual(health._check_self_heal_recurrence(state, "codex", "execution_error", current=1500.0), "fp1")
        self.assertIsNone(health._check_self_heal_recurrence(state, "codex", "execution_error", current=1000.0 + 3600 + 1))
        self.assertIsNone(health._check_self_heal_recurrence(state, "claude", "execution_error", current=1500.0))

    def test_revert_self_heal_commit_success(self):
        with mock.patch.object(health.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            result = health._revert_self_heal_commit("abc123")
        self.assertEqual(result["status"], "reverted")

    def test_revert_self_heal_commit_conflict_aborts_and_reports(self):
        def run(command, **kwargs):
            if "revert" in command and "--abort" not in command:
                return mock.Mock(returncode=1, stdout="", stderr="conflict")
            if "--abort" in command:
                return mock.Mock(returncode=0, stdout="", stderr="")
            raise AssertionError(command)
        with mock.patch.object(health.subprocess, "run", side_effect=run):
            result = health._revert_self_heal_commit("abc123")
        self.assertEqual(result["status"], "conflict")

    def test_blacklist_fingerprint_and_check(self):
        state = {"self_heal_blacklist": {}}
        self.assertFalse(health._is_blacklisted(state, "fp1"))
        health._blacklist_fingerprint(state, "fp1", "recurrence within 1h", current=2000.0)
        self.assertTrue(health._is_blacklisted(state, "fp1"))
        self.assertEqual(state["self_heal_blacklist"]["fp1"]["reason"], "recurrence within 1h")

    def test_poll_once_detects_recurrence_and_blacklists_on_clean_revert(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "codex.log"
            log.write_text("", encoding="utf-8")
            original_targets = health.TARGETS
            health.TARGETS = {"codex": {"label": "present", "log": log}}
            try:
                state = {
                    "initialized": True, "offsets": {"codex": 0}, "pending": {}, "alerted": {},
                    "incidents": {}, "usage_watch": {},
                    "self_heal_watch": {"fp1": {"role": "codex", "code": "execution_error", "merge_commit": "abc123", "watched_at": 1000.0, "deadline": 1000.0 + 3600}},
                    "self_heal_blacklist": {},
                }
                log.write_text("[codex] 처리 실패 task=task-9 error=provider subprocess crashed\n", encoding="utf-8")
                with mock.patch.object(health, "_revert_self_heal_commit", return_value={"status": "reverted", "detail": "ok"}):
                    alerts = health.poll_once(state, now=1500)
                self.assertTrue(health._is_blacklisted(state, "fp1"))
                self.assertNotIn("fp1", state["self_heal_watch"])
                self.assertTrue(any(a.get("code") == "self_heal_recurrence" for a in alerts))
            finally:
                health.TARGETS = original_targets
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k self_heal_merge -k self_heal_recurrence -k self_heal_commit -k blacklist -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

Near the other constants:

```python
SELF_HEAL_RECURRENCE_WINDOW_SECONDS = 3600
```

Above `def poll_once`:

```python
def _watch_self_heal_merge(state: dict, fingerprint: str, role: str, code: str, merge_commit: str, current: float) -> None:
    state.setdefault("self_heal_watch", {})[fingerprint] = {
        "role": role, "code": code, "merge_commit": merge_commit,
        "watched_at": current, "deadline": current + SELF_HEAL_RECURRENCE_WINDOW_SECONDS,
    }


def _check_self_heal_recurrence(state: dict, role: str, code: str, current: float) -> str | None:
    for fingerprint, watch in state.get("self_heal_watch", {}).items():
        if watch.get("role") != role or watch.get("code") != code:
            continue
        if current <= float(watch.get("deadline", 0)):
            return fingerprint
    return None


def _revert_self_heal_commit(commit: str) -> dict:
    revert = subprocess.run(
        ["/usr/bin/git", "-C", str(SOURCE_REPO), "revert", "--no-edit", commit],
        capture_output=True, text=True, check=False,
    )
    if revert.returncode == 0:
        return {"status": "reverted", "detail": "revert succeeded"}
    abort = subprocess.run(
        ["/usr/bin/git", "-C", str(SOURCE_REPO), "revert", "--abort"],
        capture_output=True, text=True, check=False,
    )
    detail = (revert.stderr or "")[-500:]
    if abort.returncode != 0:
        return {"status": "error", "detail": f"revert failed and abort also failed: {detail}"}
    return {"status": "conflict", "detail": detail}


def _blacklist_fingerprint(state: dict, fingerprint: str, reason: str, current: float) -> None:
    state.setdefault("self_heal_blacklist", {})[fingerprint] = {"reason": reason, "blacklisted_at": current}


def _is_blacklisted(state: dict, fingerprint: str) -> bool:
    return fingerprint in state.get("self_heal_blacklist", {})
```

Now wire recurrence detection into `poll_once`'s per-line classify loop, right where the code is
classified (currently line 1826-1828):

```python
            code = classify_line(line, role=role)
            _record_diagnostic_observation(state, line, role, code)
            if code:
                recurrence_fingerprint = _check_self_heal_recurrence(state, role, code, current)
                if recurrence_fingerprint is not None:
                    watch = state["self_heal_watch"].get(recurrence_fingerprint, {})
                    revert_result = _revert_self_heal_commit(str(watch.get("merge_commit", "")))
                    if revert_result["status"] == "reverted":
                        _blacklist_fingerprint(state, recurrence_fingerprint, "recurrence within 1h post-merge", current)
                    state["self_heal_watch"].pop(recurrence_fingerprint, None)
                    alerts.append({
                        "kind": "escalation_notice",
                        "role": role, "code": "self_heal_recurrence",
                        "fingerprint": f"recurrence:{recurrence_fingerprint}:{int(current)}",
                        "message": (
                            f"[Roda 재발 감지] incident={recurrence_fingerprint}의 자동치유 병합 이후 "
                            f"같은 문제(role={role}, code={code})가 다시 발생했습니다. "
                            f"revert 결과: {revert_result['status']}."
                        ),
                        "detail": revert_result["detail"],
                    })
```

(This is a minimal insertion at the top of the existing `if code:` block — the rest of that block's
existing body, from `_record_metric(state, code, current)` onward, is unchanged and continues to
execute below this new snippet, in the same order it does today.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k self_heal_merge -k self_heal_recurrence -k self_heal_commit -k blacklist -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: add self-heal post-merge recurrence detection, revert, and blacklist"
```

---

### Task 5: Dynamic implementer-chain exclusion (skip whichever robot is currently usage-limited)

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — new function above `def poll_once`.
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: `_active_usage_watch(state, role)` (existing, line 1325 — returns `tuple[str, dict] | None`, already used by `poll_once` to track in-flight usage-limit recovery per role).
- Produces: `_implementer_chain(state: dict, current: float) -> list[str]`. Task 6 iterates this list instead of a hardcoded `["codex", "claude", "antigravity"]`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_implementer_chain_default_order(self):
        state = {"usage_watch": {}}
        self.assertEqual(health._implementer_chain(state, current=1000.0), ["codex", "claude", "antigravity"])

    def test_implementer_chain_skips_role_with_active_usage_watch(self):
        state = {"usage_watch": {
            "fp-usage": {"role": "codex", "status": "waiting_for_probe"},
        }}
        self.assertEqual(health._implementer_chain(state, current=1000.0), ["claude", "antigravity"])

    def test_implementer_chain_ignores_expired_or_resolved_usage_watch(self):
        state = {"usage_watch": {
            "fp-usage": {"role": "codex", "status": "completed_success"},
        }}
        self.assertEqual(health._implementer_chain(state, current=1000.0), ["codex", "claude", "antigravity"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k implementer_chain -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

Above `def poll_once`:

```python
def _implementer_chain(state: dict, current: float) -> list[str]:
    base_order = ["codex", "claude", "antigravity"]
    busy = set()
    for role in base_order:
        active = _active_usage_watch(state, role)
        if active is not None:
            busy.add(role)
    return [role for role in base_order if role not in busy]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k implementer_chain -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: skip usage-limited implementers from the self-heal fallback chain"
```

---

### Task 6: `_attempt_self_heal` orchestrator — 5-minute budget, review gating, escalation handoff, `_process_cycle` rewire

This is the task that wires Tasks 1-5's helpers together into the actual self-heal attempt, and connects it to `_process_cycle`. It is the largest task in this plan — treat each numbered sub-step below as its own commit-sized unit if it helps keep review small, but the interface is one function.

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — new function `_attempt_self_heal` above `def poll_once`, plus new helper `_review_implementer_diff`, plus the `_process_cycle` rewrite (currently lines 1933-1989).
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: `_implementer_chain` (Task 5), `_run_implementer_cli` (Task 1), `_diff_touches_protected_files`/`_is_low_risk_diff`/`_run_full_test_suite`/`_merge_allowed` (Task 2), `_check_fingerprint_attempt_budget`/`_record_self_heal_attempt`/`_check_global_merge_budget`/`_record_self_heal_merge`/`_manual_mode_active`/`_enter_manual_mode` (Task 3), `_watch_self_heal_merge`/`_check_self_heal_recurrence`/`_is_blacklisted` (Task 4), `_merge_repair_commit_and_restart` (existing, line 1063), `REPAIR_ROOT` (existing).
- Produces: `_attempt_self_heal(event: dict, state: dict) -> bool` — `True` on confirmed self-heal success (incident resolved, no further escalation needed), `False` on failure (caller must then route into the existing `escalation_stage="awaiting_ack"` flow). Also produces `_review_implementer_diff(reviewer_role: str, diff_text: str | None, worktree: Path) -> bool` (pass/fail).

- [ ] **Step 1: Write the failing tests**

```python
    def test_attempt_self_heal_blocked_by_blacklist_returns_false_immediately(self):
        state = {
            "self_heal_blacklist": {"fp1": {"reason": "x", "blacklisted_at": 0}},
            "incidents": {"fp1": {"escalation_stage": "awaiting_ack"}},
        }
        event = {"fingerprint": "fp1", "role": "codex", "code": "execution_error", "detail": "boom"}
        with mock.patch.object(health, "_run_implementer_cli") as cli:
            result = health._attempt_self_heal(event, state)
            cli.assert_not_called()
        self.assertFalse(result)

    def test_attempt_self_heal_blocked_by_manual_mode_returns_false_immediately(self):
        state = {
            "self_heal_blacklist": {}, "self_heal_manual_mode": {"active": True, "since": 1},
            "incidents": {"fp1": {"escalation_stage": "awaiting_ack"}},
        }
        event = {"fingerprint": "fp1", "role": "codex", "code": "execution_error", "detail": "boom"}
        with mock.patch.object(health, "_run_implementer_cli") as cli:
            result = health._attempt_self_heal(event, state)
            cli.assert_not_called()
        self.assertFalse(result)

    def test_attempt_self_heal_blocked_by_fingerprint_attempt_budget(self):
        state = {
            "self_heal_blacklist": {}, "self_heal_manual_mode": {"active": False, "since": None},
            "self_heal_attempts": {"fp1": [1.0, 2.0]},
            "incidents": {"fp1": {"escalation_stage": "awaiting_ack"}},
        }
        event = {"fingerprint": "fp1", "role": "codex", "code": "execution_error", "detail": "boom"}
        with mock.patch.object(health, "_run_implementer_cli") as cli, \
                mock.patch.object(health.time, "time", return_value=3.0):
            result = health._attempt_self_heal(event, state)
            cli.assert_not_called()
        self.assertFalse(result)
        self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "awaiting_ack")

    def test_attempt_self_heal_end_to_end_success_two_reviewers(self):
        state = {
            "self_heal_blacklist": {}, "self_heal_manual_mode": {"active": False, "since": None},
            "self_heal_attempts": {}, "self_heal_merges": [], "self_heal_watch": {}, "usage_watch": {},
            "incidents": {"fp1": {"escalation_stage": "auto_repairing"}},
        }
        event = {"fingerprint": "fp1", "role": "codex", "code": "execution_error", "detail": "boom"}
        cli_result = {
            "status": "success", "diff": "+x", "changed_files": ["bin/x.py"],
            "exit_code": 0, "timed_out": False, "stderr_tail": "",
        }
        with mock.patch.object(health, "_run_implementer_cli", return_value=cli_result), \
                mock.patch.object(health, "_review_implementer_diff", return_value=True), \
                mock.patch.object(health, "_run_full_test_suite", return_value=True), \
                mock.patch.object(health, "_merge_repair_commit_and_restart", return_value="Codex 자동 수정·main 병합·codex 서비스 재기동 완료."), \
                mock.patch.object(health.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="head123\n", stderr="")), \
                mock.patch.object(health.time, "time", return_value=1000.0):
            result = health._attempt_self_heal(event, state)
        self.assertTrue(result)
        self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "resolved")
        self.assertEqual(len(state["self_heal_merges"]), 1)
        self.assertIn("fp1", state["self_heal_watch"])

    def test_attempt_self_heal_all_implementers_fail_falls_back_to_escalation(self):
        state = {
            "self_heal_blacklist": {}, "self_heal_manual_mode": {"active": False, "since": None},
            "self_heal_attempts": {}, "self_heal_merges": [], "self_heal_watch": {}, "usage_watch": {},
            "incidents": {"fp1": {"escalation_stage": "auto_repairing"}},
        }
        event = {"fingerprint": "fp1", "role": "codex", "code": "execution_error", "detail": "boom"}
        cli_result = {
            "status": "no_change", "diff": None, "changed_files": [],
            "exit_code": 0, "timed_out": False, "stderr_tail": "",
        }
        with mock.patch.object(health, "_run_implementer_cli", return_value=cli_result), \
                mock.patch.object(health.time, "time", return_value=1000.0):
            result = health._attempt_self_heal(event, state)
        self.assertFalse(result)
        incident = state["incidents"]["fp1"]
        self.assertEqual(incident["escalation_stage"], "awaiting_ack")
        self.assertEqual(incident["routed_at"], 1000.0)
        self.assertEqual(incident["ack_deadline"], 1000.0 + health.ROUTING_ACK_TIMEOUT_SECONDS)

    def test_attempt_self_heal_resets_worktree_between_failed_and_next_implementer(self):
        state = {
            "self_heal_blacklist": {}, "self_heal_manual_mode": {"active": False, "since": None},
            "self_heal_attempts": {}, "self_heal_merges": [], "self_heal_watch": {}, "usage_watch": {},
            "incidents": {"fp1": {"escalation_stage": "auto_repairing"}},
        }
        event = {"fingerprint": "fp1", "role": "codex", "code": "execution_error", "detail": "boom"}
        results_by_role = {
            "codex": {"status": "apply_failed", "diff": None, "changed_files": [], "exit_code": 1, "timed_out": False, "stderr_tail": ""},
            "claude": {"status": "no_change", "diff": None, "changed_files": [], "exit_code": 0, "timed_out": False, "stderr_tail": ""},
            "antigravity": {"status": "no_change", "diff": None, "changed_files": [], "exit_code": 0, "timed_out": False, "stderr_tail": ""},
        }
        reset_calls = []
        def fake_cli(role, prompt, worktree, timeout=180):
            return results_by_role[role]
        def fake_run(command, **kwargs):
            if "reset" in command or "clean" in command:
                reset_calls.append(command)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as td:
            original_repair_root = health.REPAIR_ROOT
            health.REPAIR_ROOT = Path(td)
            worktree = health.REPAIR_ROOT / "fp1"
            worktree.mkdir(parents=True)  # pre-existing, as if a prior attempt left it behind
            try:
                with mock.patch.object(health, "_run_implementer_cli", side_effect=fake_cli), \
                        mock.patch.object(health.subprocess, "run", side_effect=fake_run), \
                        mock.patch.object(health.time, "time", return_value=1000.0):
                    health._attempt_self_heal(event, state)
            finally:
                health.REPAIR_ROOT = original_repair_root
        # A pre-existing worktree means every implementer attempt (codex, claude,
        # antigravity) goes through the reset branch, never the worktree-add branch.
        self.assertGreaterEqual(len(reset_calls), 2)

    def test_attempt_self_heal_recurrence_after_merge_reverts_and_blacklists(self):
        # This models poll_once's classify loop detecting the SAME (role, code)
        # again while a self_heal_watch entry is still active; the merge/revert
        # logic itself is exercised through _check_self_heal_recurrence +
        # _revert_self_heal_commit directly (Task 4's own tests cover the pure
        # functions). This test only asserts the wiring inside poll_once calls
        # them and blacklists on a clean revert.
        state = {
            "self_heal_watch": {"fp1": {"role": "codex", "code": "execution_error", "merge_commit": "abc123", "watched_at": 900.0, "deadline": 900.0 + 3600}},
            "self_heal_blacklist": {},
        }
        with mock.patch.object(health, "_revert_self_heal_commit", return_value={"status": "reverted", "detail": "ok"}) as revert:
            matched = health._check_self_heal_recurrence(state, "codex", "execution_error", current=1000.0)
            self.assertEqual(matched, "fp1")
            result = health._revert_self_heal_commit(state["self_heal_watch"]["fp1"]["merge_commit"])
            if result["status"] == "reverted":
                health._blacklist_fingerprint(state, matched, "recurrence within 1h post-merge", current=1000.0)
        revert.assert_called_once_with("abc123")
        self.assertTrue(health._is_blacklisted(state, "fp1"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k attempt_self_heal -v`
Expected: FAIL — `AttributeError: module 'health' has no attribute '_attempt_self_heal'`.

- [ ] **Step 3: Implement**

Above `def poll_once`, add `_review_implementer_diff` and `_attempt_self_heal`:

```python
def _review_implementer_diff(reviewer_role: str, diff_text: str | None, worktree: Path, *, timeout: int = 180) -> bool:
    """A reviewer that is NOT the implementer judges the diff. Reuses the
    same triage-CLI dispatch pattern as _run_antigravity_triage_cli for
    antigravity, and the equivalent flag set for claude/codex review calls.
    `timeout` is the caller's remaining self-heal budget (Task 6), so review
    calls near the end of the 5-minute window get a shrinking timeout instead
    of an unconditional 180s that could push the whole attempt over budget."""
    if timeout <= 0:
        return False
    prompt = (
        "다음 diff를 검토하라. 안전하고 요청 범위 내의 최소 변경이면 정확히 "
        "`REVIEW: APPROVE`로 시작하는 한 줄을, 문제가 있으면 `REVIEW: REJECT`로 "
        "시작하는 한 줄을 첫 줄에 출력하라.\n\n" + (diff_text or "")
    )
    try:
        if reviewer_role == "codex":
            result = subprocess.run(
                [str(CODEX_BIN), "exec", "--json", "-s", "read-only", "--skip-git-repo-check", "-C", str(worktree), "--", prompt],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            output = result.stdout
        elif reviewer_role == "antigravity":
            output = _run_antigravity_triage_cli(prompt)
        elif reviewer_role == "claude":
            result = subprocess.run(
                [str(CLAUDE_BIN), "-p", "--model", "sonnet", "--effort", "medium", "--output-format", "json", prompt],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            payload = json.loads(result.stdout) if result.returncode == 0 else {}
            output = str(payload.get("result") or "")
        else:
            return False
    except (subprocess.TimeoutExpired, OSError, RuntimeError, json.JSONDecodeError):
        return False
    return bool(re.search(r"REVIEW:\s*APPROVE", output, re.I))


def _attempt_self_heal(event: dict, state: dict) -> bool:
    fingerprint = str(event["fingerprint"])
    incident = state.setdefault("incidents", {}).setdefault(fingerprint, {})
    incident["escalation_stage"] = "auto_repairing"

    def _fall_back_to_escalation() -> bool:
        now = time.time()
        incident["escalation_stage"] = "awaiting_ack"
        incident["routed_role"] = incident.get("routed_role") or event.get("role")
        incident["routed_at"] = now
        incident["ack_deadline"] = now + ROUTING_ACK_TIMEOUT_SECONDS
        incident.setdefault("reroute_count", 0)
        incident.setdefault("related_incidents", [])
        return False

    if _is_blacklisted(state, fingerprint):
        return _fall_back_to_escalation()
    if _manual_mode_active(state):
        return _fall_back_to_escalation()
    started_at = time.time()
    if not _check_fingerprint_attempt_budget(state, fingerprint, started_at):
        return _fall_back_to_escalation()
    if not _check_global_merge_budget(state, started_at):
        return _fall_back_to_escalation()

    _record_self_heal_attempt(state, fingerprint, started_at)
    chain = _implementer_chain(state, started_at)
    worktree = REPAIR_ROOT / fingerprint
    prompt = (
        "장애 원인을 파악하고 최소 범위의 안전한 개선을 구현하라. "
        "작업 worktree에서만 수정하고, 토큰·인증·.env·삭제·reset·외부 전송은 절대 수행하지 말라.\n\n"
        f"대상 provider: {event.get('role')}\n감지 코드: {event.get('code')}\n관측 세부: {event.get('detail')}\n"
    )

    for implementer_role in chain:
        elapsed = time.time() - started_at
        remaining_budget = SELF_HEAL_TOTAL_TIMEOUT_SECONDS - elapsed
        if remaining_budget <= 0:
            break
        if not worktree.exists():
            REPAIR_ROOT.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["/usr/bin/git", "-C", str(SOURCE_REPO), "worktree", "add", "--detach", str(worktree), "HEAD"],
                capture_output=True, text=True, check=False,
            )
        else:
            # A previous implementer in this same chain may have left partial,
            # failed changes behind. Reset to a clean HEAD before the next
            # implementer touches the worktree so their edits never mix.
            subprocess.run(["/usr/bin/git", "-C", str(worktree), "reset", "--hard", "HEAD"], capture_output=True, text=True, check=False)
            subprocess.run(["/usr/bin/git", "-C", str(worktree), "clean", "-fd"], capture_output=True, text=True, check=False)
        per_attempt_timeout = int(min(180, remaining_budget))
        if per_attempt_timeout <= 0:
            break
        cli_result = _run_implementer_cli(implementer_role, prompt, worktree, timeout=per_attempt_timeout)
        if cli_result["status"] != "success":
            continue

        post_implement_remaining = SELF_HEAL_TOTAL_TIMEOUT_SECONDS - (time.time() - started_at)
        if post_implement_remaining <= 0:
            break
        low_risk = _is_low_risk_diff(cli_result["changed_files"], cli_result.get("diff"))
        other_roles = [r for r in ("codex", "claude", "antigravity") if r != implementer_role]
        review_count = 0
        for reviewer_role in other_roles:
            review_timeout = int(min(180, SELF_HEAL_TOTAL_TIMEOUT_SECONDS - (time.time() - started_at)))
            if review_timeout <= 0:
                break
            if _review_implementer_diff(reviewer_role, cli_result.get("diff"), worktree, timeout=review_timeout):
                review_count += 1
            if review_count >= 2:
                break
            if review_count >= 1 and low_risk:
                break
        if SELF_HEAL_TOTAL_TIMEOUT_SECONDS - (time.time() - started_at) <= 0:
            break

        tests_passed = _run_full_test_suite()
        if not _merge_allowed(review_count, low_risk, tests_passed):
            continue

        commit = subprocess.run(["/usr/bin/git", "-C", str(worktree), "add", "-A"], capture_output=True, text=True, check=False)
        if commit.returncode != 0:
            continue
        commit = subprocess.run(
            ["/usr/bin/git", "-C", str(worktree), "commit", "-m", f"fix: automated self-heal {fingerprint}"],
            capture_output=True, text=True, check=False,
        )
        if commit.returncode != 0:
            continue
        repair_commit = subprocess.run(
            ["/usr/bin/git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True, check=False,
        ).stdout.strip()
        if not repair_commit:
            continue
        merge_result = _merge_repair_commit_and_restart(
            role=event.get("role", "unknown"), code=event.get("code", "unknown"),
            repair_commit=repair_commit, fingerprint=fingerprint,
        )
        if not _repair_succeeded(merge_result):
            continue

        now = time.time()
        _record_self_heal_merge(state, now)
        _watch_self_heal_merge(state, fingerprint, event.get("role", "unknown"), event.get("code", "unknown"), repair_commit, now)
        incident["escalation_stage"] = "resolved"
        incident["resolved_at"] = now
        incident.setdefault("self_heal_attempts_log", []).append({
            "role": implementer_role, "status": "success", "changed_files": cli_result["changed_files"],
            "review_count": review_count, "tests_passed": tests_passed, "merge_commit": repair_commit,
        })
        if _check_global_merge_budget(state, now) is False:
            _enter_manual_mode(state, now)
        return True

    return _fall_back_to_escalation()
```

Near the other constants:

```python
SELF_HEAL_TOTAL_TIMEOUT_SECONDS = 300
```

Now rewrite the `_process_cycle` repair branch (lines 1933-1989). The existing body:

```python
def _process_cycle(state: dict) -> None:
    _retry_pending_merges(state)
    alerts = poll_once(state)
    alerts.extend(_process_antigravity_triage(state))
    _save_state(state)
    for event in alerts:
        try:
            if event.get("kind") in {"recovery_result", "usage_recovery"}:
                _send_alert(f"{event['message']}\n세부: {event['detail']}")
                continue
            fingerprint = str(event["fingerprint"])
            if event.get("code") in NON_REPAIRABLE_CODES or event.get("auto_repair") == "blocked":
                ...
```

Replace the block starting at `if event.get("code") in NON_REPAIRABLE_CODES or event.get("auto_repair") == "blocked":` through the end of that `for event in alerts:` loop body (i.e. everything from that `if` down to — but not including — the `except Exception as exc:` line) with:

```python
            if event.get("code") in NON_REPAIRABLE_CODES or event.get("auto_repair") == "blocked":
                _send_alert(f"{event['message']}\n세부: {event['detail']}")
                blocked_reason = {
                    "main_dirty": "main 저장소 추적 변경 — 사람 확인 필요",
                    "usage_limited": "사용량 제한 이벤트 — 자동복구 차단",
                    "session_limited": "native CLI 세션 제한 가능성 — 계정 전체 사용량으로 단정하지 않고 fresh-session 확인 대기",
                    "rate_limited": "요청 빈도 제한 이벤트 — 자동복구 차단",
                    "capacity_limited": "provider 용량 제한 이벤트 — 자동복구 차단",
                    "service_overloaded": "provider 과부하 이벤트 — 자동복구 차단",
                    "context_exceeded": "컨텍스트 제한 이벤트 — 자동복구 차단",
                    "auth_error": "인증 오류 이벤트 — 자동복구 차단",
                }.get(event.get("code"), "자동복구 차단 이벤트")
                state["repair_results"][fingerprint] = blocked_reason
                _save_state(state)
                continue
            if event.get("kind") == "escalation_notice":
                _send_alert(event["message"])
                continue
            if fingerprint not in state.get("self_heal_watch", {}) and state["incidents"].get(fingerprint, {}).get("escalation_stage") in {None, "awaiting_ack"}:
                healed = _attempt_self_heal(event, state)
                _save_state(state)
                if not healed:
                    continue
            _send_alert(f"{event['message']}\n세부: {event['detail']}")
```

This removes the old Codex-only `_run_codex_repair`/`diagnosis`/`repair_results` dispatch from the hot alert path — `_run_codex_repair_impl`, `_run_codex_repair`, `_format_repair_result`, `_repair_preflight_blocker`, and `_repair_approval_granted` stay in the file (still used by `_retry_pending_merges` for queued merges from before this change, and by the manual-mode fallback path an operator can still trigger), but new alerts no longer call them directly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k attempt_self_heal -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS — pay particular attention to any existing `_process_cycle` test (search `grep -n "_process_cycle" tests/test_roda_telegram_health_monitor.py`); those tests assert on the OLD Codex-only dispatch and must be updated to mock `_attempt_self_heal` instead of `_run_codex_repair`, preserving their original intent (alert delivered, repair path invoked) but against the new function name.

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: wire self-heal attempt into process_cycle with escalation-chain fallback"
```

---

### Task 7: Audit trail formalization + idempotent restart guard

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — extend `_attempt_self_heal` (Task 6) to record a full structured entry per implementer attempt (not just on success), and add a restart-idempotency guard.
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: `incident["self_heal_attempts_log"]` (already introduced on the success path in Task 6; this task extends it to also log failed attempts, and adds the restart guard).
- Produces: no new function — this task deepens `_attempt_self_heal`'s existing bookkeeping and adds `_self_heal_worktree_in_progress(worktree: Path) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_attempt_self_heal_logs_every_implementer_attempt_not_just_success(self):
        state = {
            "self_heal_blacklist": {}, "self_heal_manual_mode": {"active": False, "since": None},
            "self_heal_attempts": {}, "self_heal_merges": [], "self_heal_watch": {}, "usage_watch": {},
            "incidents": {"fp1": {"escalation_stage": "auto_repairing"}},
        }
        event = {"fingerprint": "fp1", "role": "codex", "code": "execution_error", "detail": "boom"}
        results_by_role = {
            "codex": {"status": "no_change", "diff": None, "changed_files": [], "exit_code": 0, "timed_out": False, "stderr_tail": ""},
            "claude": {"status": "apply_failed", "diff": None, "changed_files": [], "exit_code": 1, "timed_out": False, "stderr_tail": "bad patch"},
            "antigravity": {"status": "timeout", "diff": None, "changed_files": [], "exit_code": None, "timed_out": True, "stderr_tail": ""},
        }
        def fake_cli(role, prompt, worktree):
            return results_by_role[role]
        with mock.patch.object(health, "_run_implementer_cli", side_effect=fake_cli), \
                mock.patch.object(health.time, "time", return_value=1000.0):
            result = health._attempt_self_heal(event, state)
        self.assertFalse(result)
        log = state["incidents"]["fp1"]["self_heal_attempts_log"]
        self.assertEqual([entry["role"] for entry in log], ["codex", "claude", "antigravity"])
        self.assertEqual([entry["status"] for entry in log], ["no_change", "apply_failed", "timeout"])

    def test_self_heal_worktree_in_progress_detects_existing_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td) / "fp1"
            self.assertFalse(health._self_heal_worktree_in_progress(worktree))
            worktree.mkdir()
            self.assertTrue(health._self_heal_worktree_in_progress(worktree))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k logs_every_implementer -k worktree_in_progress -v`
Expected: FAIL — the failure-path log assertion fails (log only has the success-path entry today), and `_self_heal_worktree_in_progress` doesn't exist yet.

- [ ] **Step 3: Implement**

Above `def poll_once`, add:

```python
def _self_heal_worktree_in_progress(worktree: Path) -> bool:
    return worktree.exists()
```

In `_attempt_self_heal` (Task 6), inside the `for implementer_role in chain:` loop, change the section that currently reads:

```python
        cli_result = _run_implementer_cli(implementer_role, prompt, worktree)
        if cli_result["status"] != "success":
            continue
```

to:

```python
        cli_result = _run_implementer_cli(implementer_role, prompt, worktree)
        if cli_result["status"] != "success":
            incident.setdefault("self_heal_attempts_log", []).append({
                "role": implementer_role, "status": cli_result["status"],
                "changed_files": cli_result["changed_files"], "review_count": 0,
                "tests_passed": None, "merge_commit": None,
            })
            continue
```

And guard the worktree-creation block right above it (currently unconditional `if not worktree.exists(): ... worktree add`) so a restart mid-cycle does not re-add a worktree that a previous, interrupted run already created and left in place — change:

```python
        if not worktree.exists():
            REPAIR_ROOT.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["/usr/bin/git", "-C", str(SOURCE_REPO), "worktree", "add", "--detach", str(worktree), "HEAD"],
                capture_output=True, text=True, check=False,
            )
```

to:

```python
        if not _self_heal_worktree_in_progress(worktree):
            REPAIR_ROOT.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["/usr/bin/git", "-C", str(SOURCE_REPO), "worktree", "add", "--detach", str(worktree), "HEAD"],
                capture_output=True, text=True, check=False,
            )
```

(This makes the guard an explicitly named, independently testable check rather than an inline `Path.exists()` call, and gives a restart-recovery seam: an operator or a future task can extend `_self_heal_worktree_in_progress` to also check for an uncommitted-but-in-flight patch without touching every call site.)

Also update the earlier success-path log entry (added in Task 6) to match this same shape for consistency — it already includes `role`/`status`/`changed_files`/`review_count`/`tests_passed`/`merge_commit`, so no change needed there; this step only adds the parallel failure-path entry above.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k logs_every_implementer -k worktree_in_progress -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: log every self-heal implementer attempt, guard against duplicate worktree creation"
```

---

## Post-plan verification

After all 7 tasks land, run the full repo suite once more:

```bash
python3 -m unittest discover -s tests -p "test_roda*.py" -v
```

Then re-read `docs/specs/2026-08-16-roda-self-heal-expansion-design.md`'s §1-9 and confirm every decision has a task:
- §1 전체 흐름 (auto_repairing → success/fail, 5분 하드타임아웃) — Task 6
- §2 구현자 실행 계약 (구조화된 반환값, 클로드 래퍼) — Task 1
- §3 자동치유 성공의 단일 정의 — Task 2, enforced inside Task 6's merge branch
- §4 보호 파일 목록 — Task 2 (`PROTECTED_FILE_PATTERNS`/`PROTECTED_PATH_SUBSTRINGS`)
- §5 병합 조건 (2인 기본, 1인 저위험 예외, 테스트 게이트) — Task 2 (`_merge_allowed`), applied in Task 6
- §6 3중 폭주 방지 장치 — Task 3
- §7 재발 대응 (revert + 블랙리스트) — Task 4 (pure functions + `poll_once` wiring at the `code = classify_line(...)` point), consumed by Task 6's alert dispatch (`fingerprint not in state.get("self_heal_watch", {})` guard prevents re-attempting self-heal on a fingerprint still under recurrence watch)
- §8 NON_REPAIRABLE_CODES 재분류 (동적 제외) — Task 5
- §9 감사·상태 복구 — Task 7

Every code-changing task above must go through this repo's verify-task gate (`bin/verify-task-orchestrator.py`) before landing — the main session may not `Edit`/`Write` these files directly.
