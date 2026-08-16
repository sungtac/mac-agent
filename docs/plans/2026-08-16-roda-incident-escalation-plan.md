# Roda Incident Escalation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: dispatch each task to a fresh subagent (this environment's Agent tool, or the Workflow tool for a deterministic multi-task pipeline — recommended) or use the executing-plans skill to work through this plan task-by-task in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This repo requires the verify-task gate for all edits.** Do not `Edit`/`Write` `bin/*.py` or `tests/*.py` directly in the main session. Save each task's diff intent to a task file and run `python3 bin/verify-task-orchestrator.py --task-file <file> --cwd <repo> --session-id <id>` (Codex implements, Claude/Antigravity review) before any change lands. See `docs/specs/2026-08-16-roda-role-escalation-design.md`'s closing note.

**Goal:** Turn a detected provider incident (`telegram-health-monitor.json`) into a chain that actually resolves — rule-based routing to the responsible agent, a 5-minute ack wait, a 24-hour completion wait, Antigravity triage on timeout (owner-down / misrouted / judgment-hard), and — only for the judgment-hard case — a full 4-agent deliberation. Duplicate incidents from the same root cause merge instead of re-escalating; a `(role, code)` pair that already reached full deliberation once and recurs within 7 days skips straight to a human alert.

**Architecture:** All new state lives inside the existing `telegram-health-monitor.json` incident ledger (`bin/roda-telegram-health-monitor.py`), reusing the file's existing constants (`NO_RESPONSE_SECONDS`, `PENDING_MERGE_TTL_SECONDS`, `ALERT_RETENTION_SECONDS`), its existing `pending`/`alerted`/`incidents` structures, and its existing `@{BOT_USERNAMES[role]}` direct-mention convention (already used at `bin/roda-telegram-health-monitor.py:1276` to instruct a bot to act) to route and ack incidents without inventing a new transport. The Antigravity 3-way triage step shells out to `agy --print` (no `--mode plan`, matching the already-fixed headless-hang precedent) directly from the health monitor process, mirroring how `_run_codex_repair_impl` already shells out to `codex exec` from the same file. Stage 5 (full deliberation) is triggered by having the triage step post a plain Telegram message containing a "회의해"-style deliberation marker plus "다같이" — the *existing* `bin/telegram-agent-bot.py` deliberation pipeline (`is_deliberation_request` + `classify_ingress(...).accepts("claude")`) picks this up with zero new code in that file. Roda's own reactive incident reporting (`bin/roda-gemma-bot.py`) is narrowed to only surface incidents that have entered this routing pipeline.

**Tech Stack:** Python 3 stdlib only (`re`, `subprocess`, `time`, `json`, `hashlib`), `unittest` + `unittest.mock` (test runner: `python3 -m unittest tests.<module> -v`), no new third-party dependencies, no new launchd services/plists.

## Global Constraints

- Every new/changed constant, field, and function documented in this plan is exact — no task may leave a `TBD`, a bare `pass`, or a comment like "add validation here".
- Reuse existing timing constants verbatim (design doc 확정 사항): ack wait = `NO_RESPONSE_SECONDS`/`RECOVERY_TIMEOUT_SECONDS` (300s), completion wait = `PENDING_MERGE_TTL_SECONDS`/`MAIN_DIRTY_ALERT_INTERVAL_SECONDS` (86400s), incident merge window = 5 minutes (same as ack wait), repeat-failure window = `ALERT_RETENTION_SECONDS` (7 days). Do not introduce new duration values.
- Owner routing table is exactly two rules (design doc 확정 사항 1): `main_dirty` → `codex` (the repo's existing auto-repair authority — see `_run_codex_repair_impl`); every other code → the `role` the failure was already observed on. No other special cases.
- Reroute-on-misrouting is capped at exactly 1 per incident (design doc: "무한루프 방지"). A second `MISROUTED` verdict for the same incident must fall through to the judgment-hard/deliberation path, never reroute again.
- The repeat-failure fast path only fires for a `(role, code)` pair that previously reached `deliberation_triggered` and recurred within `REPEAT_FAILURE_WINDOW_SECONDS` — not for any other kind of repetition (design doc 확정 사항 3, explicitly distinct from within-chain retries).
- **Ack detection is a documented heuristic, not per-request correlation**: because the routed bot's `task_id` for the alert reply is not knowable in advance, "acked" means *any* new pending task appears for the routed role after `routed_at` and before `ack_deadline`. This must be called out in code comments where implemented (Task 3) — it is intentional, not an oversight.
- No new launchd plist, no new standalone script/process. The Antigravity triage step runs inside the existing `roda-telegram-health-monitor` service loop via `_process_cycle`, exactly like the existing Codex auto-repair step.
- `poll_once()` must stay side-effect-free w.r.t. subprocess/network calls (existing invariant relied on by its unit tests) — the Antigravity CLI call and any `_send_alert` call happen only in `_process_cycle`, never in `poll_once` or its helpers.
- Do not touch `bin/telegram-agent-bot.py`, `bin/edge_agent_deliberation.py`, or `bin/edge_agent_ingress.py` — stage 5 reuses their existing, unmodified pipeline.

---

## File Structure

- **Modify `bin/roda-telegram-health-monitor.py`** (all new logic lives here):
  - `_default_state()` / `_migrate_state()` — add `state["deliberation_history"]: list[dict]`, bump `STATE_SCHEMA_VERSION` 5 → 6, default every existing incident's `escalation_stage` to `None` on migration.
  - New constants: `ROUTING_ACK_TIMEOUT_SECONDS`, `ROUTING_COMPLETION_TIMEOUT_SECONDS`, `INCIDENT_MERGE_WINDOW_SECONDS`, `REPEAT_FAILURE_WINDOW_SECONDS` (all aliases of existing constants), `AGY_BIN`, `ANTIGRAVITY_TRIAGE_ENABLED`.
  - `_route_incident(role, code) -> str` — the 2-rule owner table.
  - `_check_repeat_failure(state, role, code, current) -> bool` — consults `state["deliberation_history"]`.
  - `_find_mergeable_incident(state, role, current) -> str | None` — same-role open-incident lookup within the merge window.
  - `_route_incident_event(state, event, current) -> None` — replaces the bare `_upsert_incident(state, event, current)` call at the end of `poll_once`; orchestrates repeat-failure / merge / first-time-routing, and appends the `@{BOT_USERNAMES[...]}` mention to `event["message"]` when routing occurs.
  - Inline hook inside the existing `START_RE` branch of `poll_once`'s per-line loop — `_record_incident_ack(state, role, pending_id, current)`.
  - Inline hook inside the existing `DONE_RE` branch — `_resolve_incident_ack_completion(state, role, task_id, current)`.
  - `_check_ack_timeouts(state, current) -> list[dict]` and `_check_completion_timeouts(state, current) -> list[dict]` — called from `poll_once`, feed into its returned `alerts` list.
  - `_dispatch_antigravity_escalation(state, fingerprint, incident, reason, current) -> dict` — shared by both timeout sweeps.
  - `_build_triage_prompt(incident) -> str`, `_parse_triage_verdict(text) -> dict` — pure functions, no I/O.
  - `_run_antigravity_triage_cli(prompt) -> str` — the one function that shells out to `agy`.
  - `_apply_triage_verdict(state, fingerprint, incident, verdict, current) -> dict | None` — owner-down / reroute-once / judgment-hard branches.
  - `_process_antigravity_triage(state, *, current=None) -> list[dict]` — iterates `pending_antigravity_triage` incidents; called from `_process_cycle`.
  - `_process_cycle` — one new call site for `_process_antigravity_triage`, delivering its returned alert events the same way existing alerts are delivered.
- **Modify `bin/roda-gemma-bot.py`**:
  - `_render_unresolved_incidents` — filter to `item.get("escalation_stage")` being set (not `None`/missing), so Roda only ever reports on incidents that entered the routing pipeline.
- **Test: `tests/test_roda_telegram_health_monitor.py`** — new tests for every function above, following this file's existing `importlib.util.spec_from_file_location` / `mock.patch.object(health, ...)` conventions.
- **Test: `tests/test_roda_gemma_bot.py`** — new test for the `_render_unresolved_incidents` filter (create this file if it does not already exist; check first — see Task 7 Step 1).

---

### Task 1: State schema — escalation fields + `deliberation_history` + schema v6

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py:47` (`STATE_SCHEMA_VERSION`), `:240-261` (`_default_state`), `:272-342` (`_migrate_state`)
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Produces: `_default_state()["deliberation_history"] == []`; `_migrate_state(payload)` guarantees `state["deliberation_history"]` is a `list` and every value in `state["incidents"]` has an `"escalation_stage"` key (existing entries default to `None`, meaning "not yet processed by the routing pipeline").
- Consumes: nothing new.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_roda_telegram_health_monitor.py`, inside `class RodaHealthMonitorTests`:

```python
    def test_migration_adds_deliberation_history_and_defaults_escalation_stage(self):
        state = health._migrate_state({
            "schema_version": 5,
            "incidents": {
                "f1": {
                    "incident_id": "f1", "role": "codex", "code": "execution_error",
                    "status": "open", "first_seen_at": 100, "last_seen_at": 100,
                },
            },
        })
        self.assertEqual(state["schema_version"], 6)
        self.assertEqual(state["deliberation_history"], [])
        self.assertIsNone(state["incidents"]["f1"]["escalation_stage"])

    def test_default_state_has_empty_deliberation_history(self):
        self.assertEqual(health._default_state()["deliberation_history"], [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k deliberation_history -v`
Expected: FAIL — `KeyError: 'deliberation_history'` (or `schema_version` still `5`).

- [ ] **Step 3: Implement the schema changes**

In `bin/roda-telegram-health-monitor.py`, change line 47:

```python
STATE_SCHEMA_VERSION = 6
```

In `_default_state()` (around line 240), add the new key next to `"incidents": {}`:

```python
        "incidents": {},
        "deliberation_history": [],
```

In `_migrate_state()`, after the existing `for key in (... "incidents"):` dict-type-normalization loop (around line 279-281), add:

```python
    if not isinstance(state.get("deliberation_history"), list):
        state["deliberation_history"] = []
```

And right before the existing `_coalesce_specific_incidents(state)` call at the end of `_migrate_state` (line 341), add:

```python
    for incident in state.get("incidents", {}).values():
        incident.setdefault("escalation_stage", None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k deliberation_history -v`
Expected: PASS

- [ ] **Step 5: Run the full existing suite to check for regressions**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS (schema version bump must not break `test_migration_coalesces_auth_and_supersedes_recent_generic_error`, which asserts `state["schema_version"] == 5` at line 87 of the test file — update that one assertion to `6` as part of this task, since it is this task's migration behavior that changes it).

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: add escalation schema fields to health monitor state (v6)"
```

---

### Task 2: Owner routing, same-role merge, repeat-failure fast path

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — new constants near line 48 (after `ALERT_RETENTION_SECONDS`), new functions placed just above `def poll_once` (line 1434), and the call-site change inside `poll_once` at line 1595-1596.
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: `state["incidents"]`, `state["deliberation_history"]` (Task 1), `BOT_USERNAMES` (existing, line 66).
- Produces:
  - `_route_incident(role: str, code: str) -> str`
  - `_check_repeat_failure(state: dict, role: str, code: str, current: float) -> bool`
  - `_find_mergeable_incident(state: dict, role: str, current: float) -> str | None`
  - `_route_incident_event(state: dict, event: dict, current: float) -> None` — mutates `state` and, when it routes or fast-paths, mutates `event["message"]` in place by appending routing text. Later tasks (3-6) read/write the same incident fields this task creates: `escalation_stage`, `routed_role`, `routed_at`, `ack_deadline`, `reroute_count`, `attached_to`, `related_incidents`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_roda_telegram_health_monitor.py`:

```python
    def test_route_incident_sends_main_dirty_to_codex_and_others_to_their_own_role(self):
        self.assertEqual(health._route_incident("claude", "main_dirty"), "codex")
        self.assertEqual(health._route_incident("antigravity", "main_dirty"), "codex")
        self.assertEqual(health._route_incident("claude", "execution_error"), "claude")
        self.assertEqual(health._route_incident("antigravity", "service_down"), "antigravity")

    def test_route_incident_event_routes_new_failure_and_appends_mention(self):
        state = {"incidents": {}, "alerted": {}, "deliberation_history": []}
        event = {
            "role": "codex", "code": "execution_error", "fingerprint": "fp1",
            "message": "[Roda 감지] codex 봇에 execution_error 문제가 발생했습니다. 확인이 필요합니다.",
            "detail": "boom",
        }
        health._route_incident_event(state, event, current=1000.0)
        incident = state["incidents"]["fp1"]
        self.assertEqual(incident["escalation_stage"], "awaiting_ack")
        self.assertEqual(incident["routed_role"], "codex")
        self.assertEqual(incident["routed_at"], 1000.0)
        self.assertEqual(incident["ack_deadline"], 1000.0 + health.ROUTING_ACK_TIMEOUT_SECONDS)
        self.assertEqual(incident["reroute_count"], 0)
        self.assertIn(f"@{health.BOT_USERNAMES['codex']}", event["message"])

    def test_route_incident_event_merges_same_role_incident_within_window(self):
        state = {
            "incidents": {
                "primary": {
                    "incident_id": "primary", "role": "codex", "code": "execution_error",
                    "status": "open", "first_seen_at": 1000, "last_seen_at": 1000,
                    "escalation_stage": "awaiting_ack", "routed_role": "codex",
                    "routed_at": 1000, "ack_deadline": 1300, "reroute_count": 0,
                    "related_incidents": [],
                },
            },
            "alerted": {}, "deliberation_history": [],
        }
        event = {
            "role": "codex", "code": "service_down", "fingerprint": "secondary",
            "message": "[Roda 감지] codex 봇에 service_down 문제가 발생했습니다. 확인이 필요합니다.",
            "detail": "proc dead",
        }
        health._route_incident_event(state, event, current=1100.0)
        self.assertEqual(state["incidents"]["secondary"]["escalation_stage"], "attached")
        self.assertEqual(state["incidents"]["secondary"]["attached_to"], "primary")
        self.assertIn("secondary", state["incidents"]["primary"]["related_incidents"])
        self.assertNotIn("routed_role", state["incidents"]["secondary"])

    def test_route_incident_event_skips_already_routed_incident(self):
        state = {
            "incidents": {
                "fp1": {
                    "incident_id": "fp1", "role": "codex", "code": "execution_error",
                    "status": "open", "first_seen_at": 1000, "last_seen_at": 1000,
                    "escalation_stage": "awaiting_ack", "routed_role": "codex",
                    "routed_at": 1000, "ack_deadline": 1300, "reroute_count": 0,
                    "related_incidents": [],
                },
            },
            "alerted": {}, "deliberation_history": [],
        }
        event = {
            "role": "codex", "code": "execution_error", "fingerprint": "fp1",
            "message": "재발", "detail": "boom again",
        }
        health._route_incident_event(state, event, current=1050.0)
        self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "awaiting_ack")
        self.assertEqual(state["incidents"]["fp1"]["routed_at"], 1000)
        self.assertEqual(event["message"], "재발")

    def test_repeat_failure_within_window_skips_routing_and_notifies_human(self):
        state = {
            "incidents": {},
            "alerted": {},
            "deliberation_history": [
                {"role": "codex", "code": "execution_error", "triggered_at": 1000.0},
            ],
        }
        event = {
            "role": "codex", "code": "execution_error", "fingerprint": "fp2",
            "message": "[Roda 감지] codex 봇에 execution_error 문제가 발생했습니다. 확인이 필요합니다.",
            "detail": "boom",
        }
        current = 1000.0 + health.REPEAT_FAILURE_WINDOW_SECONDS - 1
        health._route_incident_event(state, event, current=current)
        incident = state["incidents"]["fp2"]
        self.assertEqual(incident["escalation_stage"], "human_notified")
        self.assertEqual(incident["escalation_reason"], "repeat_failure")
        self.assertNotIn("routed_role", incident)
        self.assertIn("반복", event["message"])

    def test_repeat_failure_outside_window_routes_normally(self):
        state = {
            "incidents": {},
            "alerted": {},
            "deliberation_history": [
                {"role": "codex", "code": "execution_error", "triggered_at": 1000.0},
            ],
        }
        event = {
            "role": "codex", "code": "execution_error", "fingerprint": "fp3",
            "message": "[Roda 감지] codex 봇에 execution_error 문제가 발생했습니다. 확인이 필요합니다.",
            "detail": "boom",
        }
        current = 1000.0 + health.REPEAT_FAILURE_WINDOW_SECONDS + 1
        health._route_incident_event(state, event, current=current)
        self.assertEqual(state["incidents"]["fp3"]["escalation_stage"], "awaiting_ack")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k "route_incident or repeat_failure" -v`
Expected: FAIL — `AttributeError: module 'health' has no attribute '_route_incident'`.

- [ ] **Step 3: Implement**

In `bin/roda-telegram-health-monitor.py`, after line 48 (`ALERT_RETENTION_SECONDS = ...`), add:

```python
# Stage-1..3 escalation reuses the existing timing constants verbatim (design
# doc 2026-08-16-roda-role-escalation-design.md, 확정 사항 1-3) — these are
# aliases, not new tunables.
ROUTING_ACK_TIMEOUT_SECONDS = NO_RESPONSE_SECONDS
ROUTING_COMPLETION_TIMEOUT_SECONDS = PENDING_MERGE_TTL_SECONDS
INCIDENT_MERGE_WINDOW_SECONDS = NO_RESPONSE_SECONDS
REPEAT_FAILURE_WINDOW_SECONDS = ALERT_RETENTION_SECONDS
```

Just above `def poll_once(state: dict, *, now: float | None = None) -> list[dict]:` (line 1434), add:

```python
def _route_incident(role: str, code: str) -> str:
    """Fixed owner table (design doc 확정 사항 1, rule 1).

    ``main_dirty`` belongs to the repo's existing auto-repair authority
    (Codex — see ``_run_codex_repair_impl``); every other code belongs to
    the role the failure was already observed on.
    """
    if code == "main_dirty":
        return "codex"
    return role


def _check_repeat_failure(state: dict, role: str, code: str, current: float) -> bool:
    """Same (role, code) already reached full deliberation within the
    repeat-failure window (design doc 확정 사항 3)."""
    for record in state.get("deliberation_history", []):
        if record.get("role") != role or record.get("code") != code:
            continue
        try:
            triggered_at = float(record.get("triggered_at", 0))
        except (TypeError, ValueError):
            continue
        if current - triggered_at <= REPEAT_FAILURE_WINDOW_SECONDS:
            return True
    return False


def _find_mergeable_incident(state: dict, role: str, current: float) -> str | None:
    """An open, already-routed, non-attached incident for the same role
    seen within the merge window (design doc 확정 사항 2)."""
    candidates = []
    for fingerprint, incident in state.get("incidents", {}).items():
        if incident.get("role") != role:
            continue
        if incident.get("status") not in {"open", "reopened"}:
            continue
        if incident.get("escalation_stage") in (None, "attached"):
            continue
        try:
            last_seen = float(incident.get("last_seen_at", 0))
        except (TypeError, ValueError):
            continue
        if current - last_seen <= INCIDENT_MERGE_WINDOW_SECONDS:
            candidates.append((last_seen, fingerprint))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _route_incident_event(state: dict, event: dict, current: float) -> None:
    if event.get("kind") in {"recovery_result", "usage_recovery", "escalation_notice"}:
        _upsert_incident(state, event, current)
        return
    fingerprint = str(event.get("fingerprint") or "")
    is_new = fingerprint not in state.get("incidents", {})
    _upsert_incident(state, event, current)
    incident = state["incidents"].get(fingerprint)
    if incident is None or not is_new:
        return
    role = str(event.get("role") or "unknown")
    code = str(event.get("code") or "unknown")
    if _check_repeat_failure(state, role, code, current):
        incident["escalation_stage"] = "human_notified"
        incident["escalation_reason"] = "repeat_failure"
        incident["escalated_at"] = current
        event["message"] += (
            f"\n\n⚠️ 같은 문제({role}/{code})가 {REPEAT_FAILURE_WINDOW_SECONDS // 86400}일 내 "
            "전체 디벨리버레이션까지 갔다가 반복되었습니다. 에이전트 체인을 건너뛰고 사람 확인이 필요합니다."
        )
        return
    merge_target = _find_mergeable_incident(state, role, current)
    if merge_target is not None:
        incident["escalation_stage"] = "attached"
        incident["attached_to"] = merge_target
        primary = state["incidents"][merge_target]
        primary.setdefault("related_incidents", [])
        if fingerprint not in primary["related_incidents"]:
            primary["related_incidents"].append(fingerprint)
        return
    routed_role = _route_incident(role, code)
    incident["escalation_stage"] = "awaiting_ack"
    incident["routed_role"] = routed_role
    incident["routed_at"] = current
    incident["ack_deadline"] = current + ROUTING_ACK_TIMEOUT_SECONDS
    incident["reroute_count"] = 0
    incident["related_incidents"] = []
    event["message"] += f"\n\n@{BOT_USERNAMES.get(routed_role, routed_role)} 확인 요망 — 이 인시던트의 담당자입니다."
```

Finally, change the call site at the end of `poll_once` (around line 1595-1596):

```python
    for event in alerts:
        _route_incident_event(state, event, current)
```

(replacing `_upsert_incident(state, event, current)`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k "route_incident or repeat_failure" -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: route new incidents to owner, merge same-role duplicates, fast-path repeats"
```

---

### Task 3: Ack detection (inline hook + timeout sweep + Antigravity dispatch helper)

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — inline hook inside the `START_RE` branch of `poll_once`'s per-line loop (around line 1479-1481), new sweep function placed above `poll_once`, new call site inside `poll_once` (around line 1448-1449, alongside `_prune_alerted`/`_expire_usage_watches`).
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: incident fields from Task 2 (`escalation_stage`, `routed_role`, `routed_at`, `ack_deadline`).
- Produces:
  - `_record_incident_ack(state: dict, role: str, pending_id: str, current: float) -> None`
  - `_dispatch_antigravity_escalation(state: dict, fingerprint: str, incident: dict, reason: str, current: float) -> dict` — returns an alert-event dict (`kind="escalation_notice"`), also used by Task 4.
  - `_check_ack_timeouts(state: dict, current: float) -> list[dict]`
  - New incident fields written: `ack_task_id`, `acked_at`, `completion_deadline` (set on ack, consumed by Task 4).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_roda_telegram_health_monitor.py`:

```python
    def test_record_incident_ack_flips_awaiting_ack_to_acked(self):
        state = {"incidents": {
            "fp1": {
                "incident_id": "fp1", "role": "codex", "code": "execution_error",
                "status": "open", "escalation_stage": "awaiting_ack",
                "routed_role": "codex", "routed_at": 1000.0, "ack_deadline": 1300.0,
            },
        }}
        health._record_incident_ack(state, "codex", "task-99", current=1050.0)
        incident = state["incidents"]["fp1"]
        self.assertEqual(incident["escalation_stage"], "acked")
        self.assertEqual(incident["ack_task_id"], "task-99")
        self.assertEqual(incident["acked_at"], 1050.0)
        self.assertEqual(incident["completion_deadline"], 1050.0 + health.ROUTING_COMPLETION_TIMEOUT_SECONDS)

    def test_record_incident_ack_ignores_unrelated_role_and_stage(self):
        state = {"incidents": {
            "fp1": {"role": "codex", "escalation_stage": "awaiting_ack", "routed_role": "claude"},
            "fp2": {"role": "codex", "escalation_stage": "acked", "routed_role": "codex"},
        }}
        health._record_incident_ack(state, "codex", "task-99", current=1050.0)
        self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "awaiting_ack")
        self.assertEqual(state["incidents"]["fp2"]["escalation_stage"], "acked")
        self.assertNotIn("ack_task_id", state["incidents"]["fp2"])

    def test_poll_once_acks_incident_when_routed_role_starts_new_task(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "codex.log"
            log.write_text("", encoding="utf-8")
            original_targets = health.TARGETS
            health.TARGETS = {"codex": {"label": "present", "log": log}}
            try:
                state = {
                    "initialized": True, "offsets": {"codex": 0}, "pending": {},
                    "alerted": {}, "incidents": {
                        "fp1": {
                            "incident_id": "fp1", "role": "codex", "code": "execution_error",
                            "status": "open", "escalation_stage": "awaiting_ack",
                            "routed_role": "codex", "routed_at": 1000.0, "ack_deadline": 1300.0,
                        },
                    },
                }
                log.write_text("[codex] 처리 시작 task=task-1\n", encoding="utf-8")
                health.poll_once(state, now=1050)
                self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "acked")
                self.assertEqual(state["incidents"]["fp1"]["ack_task_id"], "task-1")
            finally:
                health.TARGETS = original_targets

    def test_check_ack_timeouts_dispatches_antigravity_escalation(self):
        state = {"incidents": {
            "fp1": {
                "incident_id": "fp1", "role": "codex", "code": "execution_error",
                "status": "open", "escalation_stage": "awaiting_ack",
                "routed_role": "codex", "routed_at": 1000.0, "ack_deadline": 1300.0,
                "detail": "boom",
            },
        }}
        events = health._check_ack_timeouts(state, current=1301.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "escalation_notice")
        self.assertIn(f"@{health.BOT_USERNAMES['antigravity']}", events[0]["message"])
        incident = state["incidents"]["fp1"]
        self.assertEqual(incident["escalation_stage"], "pending_antigravity_triage")
        self.assertEqual(incident["escalation_reason"], "no_ack")
        self.assertEqual(incident["escalated_at"], 1301.0)

    def test_check_ack_timeouts_ignores_incidents_before_deadline(self):
        state = {"incidents": {
            "fp1": {
                "role": "codex", "escalation_stage": "awaiting_ack",
                "routed_role": "codex", "routed_at": 1000.0, "ack_deadline": 1300.0,
            },
        }}
        self.assertEqual(health._check_ack_timeouts(state, current=1299.0), [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k "incident_ack or ack_timeouts" -v`
Expected: FAIL — `AttributeError: module 'health' has no attribute '_record_incident_ack'`.

- [ ] **Step 3: Implement**

Above `def poll_once` (near the other new functions from Task 2), add:

```python
def _record_incident_ack(state: dict, role: str, pending_id: str, current: float) -> None:
    """Any new pending task for the routed role counts as an ack.

    The exact task_id the routed bot will use to answer an incident mention
    cannot be predicted in advance (it is assigned by that bot's own bridge
    when it starts handling the message), so ack detection is deliberately
    coarse: activity from the routed role after ``routed_at`` and before
    ``ack_deadline`` is treated as acknowledgement. Completion (Task 4) is
    then tracked precisely against this specific ``pending_id``.
    """
    for incident in state.get("incidents", {}).values():
        if incident.get("routed_role") != role or incident.get("escalation_stage") != "awaiting_ack":
            continue
        incident["escalation_stage"] = "acked"
        incident["ack_task_id"] = pending_id
        incident["acked_at"] = current
        incident["completion_deadline"] = current + ROUTING_COMPLETION_TIMEOUT_SECONDS


def _dispatch_antigravity_escalation(state: dict, fingerprint: str, incident: dict, reason: str, current: float) -> dict:
    incident["escalation_stage"] = "pending_antigravity_triage"
    incident["escalation_reason"] = reason
    incident["escalated_at"] = current
    reason_label = {"no_ack": "5분 내 담당자 응답 없음", "no_completion": "24시간 내 처리 완료 확인 없음"}.get(reason, reason)
    return {
        "kind": "escalation_notice",
        "role": incident.get("role", "unknown"),
        "code": "escalation",
        "fingerprint": f"escalation:{fingerprint}:{reason}:{int(current)}",
        "message": (
            f"[Roda 에스컬레이션] incident={fingerprint} (role={incident.get('role')}, code={incident.get('code')})\n"
            f"사유: {reason_label}\n세부: {incident.get('detail', '')}\n\n"
            f"@{BOT_USERNAMES.get('antigravity', 'antigravity')} 판단 요청 — "
            "담당자 다운/매핑 오류/판단 어려움 중 하나로 감별해 주세요."
        ),
        "detail": f"incident={fingerprint}; reason={reason}",
    }


def _check_ack_timeouts(state: dict, current: float) -> list[dict]:
    events = []
    for fingerprint, incident in state.get("incidents", {}).items():
        if incident.get("escalation_stage") != "awaiting_ack":
            continue
        try:
            deadline = float(incident.get("ack_deadline", 0))
        except (TypeError, ValueError):
            continue
        if current >= deadline:
            events.append(_dispatch_antigravity_escalation(state, fingerprint, incident, "no_ack", current))
    return events
```

Inside `poll_once`'s per-line loop, in the existing `START_RE` branch (around line 1479-1481):

```python
            if START_RE.search(line):
                pending_id = task_id or f"legacy-{role}-{int(current)}-{line_index}"
                role_pending[pending_id] = current
                _record_incident_ack(state, role, pending_id, current)
```

Near the top of `poll_once`, right after the existing `_expire_usage_watches(state, current)` call (line 1449), add:

```python
    alerts.extend(_check_ack_timeouts(state, current))
```

(Note: `alerts` at this point already exists as `alerts: list[dict] = []` from line 1436 — this extends it before the retry/main-dirty/per-role sections that follow, which is fine since ordering within the returned list is not asserted by any existing test.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k "incident_ack or ack_timeouts" -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: detect incident acks and escalate to Antigravity on ack timeout"
```

---

### Task 4: Completion detection (precise, keyed on `ack_task_id`)

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — inline hook inside the `DONE_RE` branch of `poll_once`'s per-line loop (around line 1482-1485), new sweep function above `poll_once`, new call site inside `poll_once`.
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: `ack_task_id`, `completion_deadline` (Task 3); reuses `_dispatch_antigravity_escalation` (Task 3).
- Produces:
  - `_resolve_incident_ack_completion(state: dict, role: str, task_id: str, current: float) -> None`
  - `_check_completion_timeouts(state: dict, current: float) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
    def test_resolve_incident_ack_completion_flips_acked_to_completed(self):
        state = {"incidents": {
            "fp1": {
                "role": "codex", "escalation_stage": "acked",
                "routed_role": "codex", "ack_task_id": "task-1",
                "completion_deadline": 90000.0,
            },
        }}
        health._resolve_incident_ack_completion(state, "codex", "task-1", current=2000.0)
        incident = state["incidents"]["fp1"]
        self.assertEqual(incident["escalation_stage"], "completed")
        self.assertEqual(incident["completed_at"], 2000.0)

    def test_resolve_incident_ack_completion_ignores_unrelated_task_id(self):
        state = {"incidents": {
            "fp1": {
                "role": "codex", "escalation_stage": "acked",
                "routed_role": "codex", "ack_task_id": "task-1",
                "completion_deadline": 90000.0,
            },
        }}
        health._resolve_incident_ack_completion(state, "codex", "task-OTHER", current=2000.0)
        self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "acked")

    def test_poll_once_completes_incident_when_ack_task_finishes(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "codex.log"
            log.write_text("", encoding="utf-8")
            original_targets = health.TARGETS
            health.TARGETS = {"codex": {"label": "present", "log": log}}
            try:
                state = {
                    "initialized": True, "offsets": {"codex": 0}, "pending": {},
                    "alerted": {}, "incidents": {
                        "fp1": {
                            "role": "codex", "escalation_stage": "acked",
                            "routed_role": "codex", "ack_task_id": "task-1",
                            "completion_deadline": 90000.0,
                        },
                    },
                }
                log.write_text("[codex] 처리 완료 task=task-1\n", encoding="utf-8")
                health.poll_once(state, now=2000)
                self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "completed")
            finally:
                health.TARGETS = original_targets

    def test_check_completion_timeouts_dispatches_antigravity_escalation(self):
        state = {"incidents": {
            "fp1": {
                "role": "codex", "code": "execution_error", "escalation_stage": "acked",
                "routed_role": "codex", "ack_task_id": "task-1",
                "completion_deadline": 90000.0, "detail": "boom",
            },
        }}
        events = health._check_completion_timeouts(state, current=90001.0)
        self.assertEqual(len(events), 1)
        incident = state["incidents"]["fp1"]
        self.assertEqual(incident["escalation_stage"], "pending_antigravity_triage")
        self.assertEqual(incident["escalation_reason"], "no_completion")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k "ack_completion or completion_timeouts" -v`
Expected: FAIL — `AttributeError: module 'health' has no attribute '_resolve_incident_ack_completion'`.

- [ ] **Step 3: Implement**

Above `def poll_once`, alongside Task 3's functions:

```python
def _resolve_incident_ack_completion(state: dict, role: str, task_id: str, current: float) -> None:
    if not task_id:
        return
    for incident in state.get("incidents", {}).values():
        if (
            incident.get("routed_role") == role
            and incident.get("escalation_stage") == "acked"
            and incident.get("ack_task_id") == task_id
        ):
            incident["escalation_stage"] = "completed"
            incident["completed_at"] = current


def _check_completion_timeouts(state: dict, current: float) -> list[dict]:
    events = []
    for fingerprint, incident in state.get("incidents", {}).items():
        if incident.get("escalation_stage") != "acked":
            continue
        try:
            deadline = float(incident.get("completion_deadline", 0))
        except (TypeError, ValueError):
            continue
        if current >= deadline:
            events.append(_dispatch_antigravity_escalation(state, fingerprint, incident, "no_completion", current))
    return events
```

Inside `poll_once`'s `DONE_RE` branch (around line 1482-1485), the existing code is:

```python
            if DONE_RE.search(line):
                if task_id:
                    role_pending.pop(task_id, None)
                    _resolve_task_incidents(state, role, task_id, current)
                else:
```

Add the new call right after the existing `_resolve_task_incidents` call:

```python
            if DONE_RE.search(line):
                if task_id:
                    role_pending.pop(task_id, None)
                    _resolve_task_incidents(state, role, task_id, current)
                    _resolve_incident_ack_completion(state, role, task_id, current)
                else:
```

Add the sweep call in `poll_once`, right next to the Task 3 sweep call added after `_expire_usage_watches`:

```python
    alerts.extend(_check_ack_timeouts(state, current))
    alerts.extend(_check_completion_timeouts(state, current))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k "ack_completion or completion_timeouts" -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: track precise incident completion via ack_task_id, escalate on timeout"
```

---

### Task 5: Antigravity triage — prompt and verdict parsing (pure functions)

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — new functions placed above `def poll_once`.
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: incident dict shape from Tasks 1-4.
- Produces:
  - `_build_triage_prompt(incident: dict) -> str`
  - `_parse_triage_verdict(text: str) -> dict` — returns `{"verdict": "OWNER_DOWN" | "MISROUTED" | "JUDGMENT_HARD", "owner": str | None}`. Any unparseable or ambiguous input must resolve to `{"verdict": "JUDGMENT_HARD", "owner": None}` (fail-safe toward full deliberation, never toward silently doing nothing).

- [ ] **Step 1: Write the failing tests**

```python
    def test_build_triage_prompt_includes_incident_context(self):
        incident = {
            "role": "codex", "code": "execution_error", "routed_role": "codex",
            "escalation_reason": "no_ack", "detail": "boom", "reroute_count": 0,
        }
        prompt = health._build_triage_prompt(incident)
        self.assertIn("codex", prompt)
        self.assertIn("execution_error", prompt)
        self.assertIn("no_ack", prompt)
        self.assertIn("VERDICT:", prompt)
        self.assertIn("OWNER_DOWN", prompt)
        self.assertIn("MISROUTED", prompt)
        self.assertIn("JUDGMENT_HARD", prompt)

    def test_parse_triage_verdict_owner_down(self):
        result = health._parse_triage_verdict("분석 결과\nVERDICT: OWNER_DOWN\n이유: codex 프로세스가 죽어있음")
        self.assertEqual(result, {"verdict": "OWNER_DOWN", "owner": None})

    def test_parse_triage_verdict_misrouted_with_owner(self):
        result = health._parse_triage_verdict("VERDICT: MISROUTED\nOWNER: claude\n이유: 실제로는 claude 담당")
        self.assertEqual(result, {"verdict": "MISROUTED", "owner": "claude"})

    def test_parse_triage_verdict_judgment_hard(self):
        result = health._parse_triage_verdict("VERDICT: JUDGMENT_HARD\n이유: 애매함")
        self.assertEqual(result, {"verdict": "JUDGMENT_HARD", "owner": None})

    def test_parse_triage_verdict_misrouted_without_owner_falls_back_to_judgment_hard(self):
        result = health._parse_triage_verdict("VERDICT: MISROUTED\n이유: 담당자 불명")
        self.assertEqual(result, {"verdict": "JUDGMENT_HARD", "owner": None})

    def test_parse_triage_verdict_misrouted_with_invalid_owner_falls_back_to_judgment_hard(self):
        result = health._parse_triage_verdict("VERDICT: MISROUTED\nOWNER: nobody\n이유: ?")
        self.assertEqual(result, {"verdict": "JUDGMENT_HARD", "owner": None})

    def test_parse_triage_verdict_unparseable_falls_back_to_judgment_hard(self):
        self.assertEqual(health._parse_triage_verdict(""), {"verdict": "JUDGMENT_HARD", "owner": None})
        self.assertEqual(health._parse_triage_verdict("정체불명의 응답"), {"verdict": "JUDGMENT_HARD", "owner": None})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k triage_prompt or triage_verdict -v`
Expected: FAIL — `AttributeError: module 'health' has no attribute '_build_triage_prompt'`.

- [ ] **Step 3: Implement**

Above `def poll_once`, alongside the other new functions:

```python
_TRIAGE_VERDICT_RE = re.compile(r"VERDICT:\s*(OWNER_DOWN|MISROUTED|JUDGMENT_HARD)", re.I)
_TRIAGE_OWNER_RE = re.compile(r"OWNER:\s*(claude|codex|antigravity)", re.I)
_TRIAGE_VALID_OWNERS = frozenset({"claude", "codex", "antigravity"})


def _build_triage_prompt(incident: dict) -> str:
    return (
        "다음 인시던트를 감별하라. 정확히 아래 세 가지 중 하나로만 판정하고, "
        "반드시 첫 줄에 `VERDICT: <값>` 형식으로 답하라 (값은 OWNER_DOWN, MISROUTED, "
        "JUDGMENT_HARD 중 하나). MISROUTED인 경우 두 번째 줄에 반드시 "
        "`OWNER: <claude|codex|antigravity>` 형식으로 실제 담당자를 지정하라.\n\n"
        "- OWNER_DOWN: 라우팅된 담당자 자체가 다운/에러 상태라 응답할 수 없는 경우\n"
        "- MISROUTED: 매핑이 틀렸고 실제 책임자가 다른 경우 (OWNER 지정 필수)\n"
        "- JUDGMENT_HARD: 담당자는 멀쩡하지만 원인 판단이 어려운 경우\n\n"
        f"인시던트 role: {incident.get('role')}\n"
        f"감지 코드: {incident.get('code')}\n"
        f"라우팅된 담당자: {incident.get('routed_role')}\n"
        f"에스컬레이션 사유: {incident.get('escalation_reason')}\n"
        f"재라우팅 횟수: {incident.get('reroute_count', 0)}\n"
        f"세부: {incident.get('detail', '')}\n"
    )


def _parse_triage_verdict(text: str) -> dict:
    match = _TRIAGE_VERDICT_RE.search(text or "")
    if not match:
        return {"verdict": "JUDGMENT_HARD", "owner": None}
    verdict = match.group(1).upper()
    if verdict != "MISROUTED":
        return {"verdict": verdict, "owner": None}
    owner_match = _TRIAGE_OWNER_RE.search(text or "")
    if not owner_match:
        return {"verdict": "JUDGMENT_HARD", "owner": None}
    owner = owner_match.group(1).lower()
    if owner not in _TRIAGE_VALID_OWNERS:
        return {"verdict": "JUDGMENT_HARD", "owner": None}
    return {"verdict": "MISROUTED", "owner": owner}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k triage_prompt or triage_verdict -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: add Antigravity triage prompt builder and fail-safe verdict parser"
```

---

### Task 6: Antigravity triage — CLI call, verdict application, deliberation trigger, `_process_cycle` wiring

**Files:**
- Modify: `bin/roda-telegram-health-monitor.py` — new constants near line 62-66 (`AGY_BIN`, `ANTIGRAVITY_TRIAGE_ENABLED`), new functions above `def poll_once`, new call site inside `_process_cycle` (around line 1601-1608).
- Test: `tests/test_roda_telegram_health_monitor.py`

**Interfaces:**
- Consumes: `_build_triage_prompt`, `_parse_triage_verdict` (Task 5); `state["deliberation_history"]` (Task 1); `ROUTING_ACK_TIMEOUT_SECONDS` (Task 2).
- Produces:
  - `_run_antigravity_triage_cli(prompt: str) -> str` — the sole subprocess call site.
  - `_apply_triage_verdict(state: dict, fingerprint: str, incident: dict, verdict: dict, current: float) -> dict | None`
  - `_process_antigravity_triage(state: dict, *, current: float | None = None) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
    def test_run_antigravity_triage_cli_invokes_agy_print_without_mode_plan(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=0, stdout="VERDICT: JUDGMENT_HARD\n", stderr="")

        with mock.patch.object(health.subprocess, "run", side_effect=run):
            output = health._run_antigravity_triage_cli("prompt text")
        self.assertEqual(output, "VERDICT: JUDGMENT_HARD\n")
        command = calls[0]
        self.assertIn(str(health.AGY_BIN), command)
        self.assertIn("--print", command)
        self.assertNotIn("--mode", command)

    def test_run_antigravity_triage_cli_raises_on_nonzero_exit(self):
        with mock.patch.object(health.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="boom")):
            with self.assertRaises(RuntimeError):
                health._run_antigravity_triage_cli("prompt text")

    def test_apply_triage_verdict_owner_down_notifies_human(self):
        state = {"incidents": {}, "deliberation_history": []}
        incident = {"role": "codex", "code": "execution_error", "reroute_count": 0}
        event = health._apply_triage_verdict(state, "fp1", incident, {"verdict": "OWNER_DOWN", "owner": None}, current=5000.0)
        self.assertEqual(incident["escalation_stage"], "human_notified")
        self.assertEqual(incident["escalation_reason"], "owner_down")
        self.assertIsNotNone(event)
        self.assertNotIn("@", event["message"].split("\n")[0])

    def test_apply_triage_verdict_misrouted_reroutes_once(self):
        state = {"incidents": {}, "deliberation_history": []}
        incident = {"role": "codex", "code": "execution_error", "routed_role": "codex", "reroute_count": 0}
        event = health._apply_triage_verdict(
            state, "fp1", incident, {"verdict": "MISROUTED", "owner": "claude"}, current=5000.0,
        )
        self.assertEqual(incident["escalation_stage"], "awaiting_ack")
        self.assertEqual(incident["routed_role"], "claude")
        self.assertEqual(incident["routed_at"], 5000.0)
        self.assertEqual(incident["ack_deadline"], 5000.0 + health.ROUTING_ACK_TIMEOUT_SECONDS)
        self.assertEqual(incident["reroute_count"], 1)
        self.assertIn(f"@{health.BOT_USERNAMES['claude']}", event["message"])

    def test_apply_triage_verdict_misrouted_second_time_falls_back_to_deliberation(self):
        state = {"incidents": {}, "deliberation_history": []}
        incident = {"role": "codex", "code": "execution_error", "routed_role": "claude", "reroute_count": 1}
        event = health._apply_triage_verdict(
            state, "fp1", incident, {"verdict": "MISROUTED", "owner": "antigravity"}, current=5000.0,
        )
        self.assertEqual(incident["escalation_stage"], "deliberation_triggered")
        self.assertEqual(len(state["deliberation_history"]), 1)
        self.assertIn("회의해", event["message"])
        self.assertIn("다같이", event["message"])

    def test_apply_triage_verdict_judgment_hard_triggers_deliberation_and_records_history(self):
        state = {"incidents": {}, "deliberation_history": []}
        incident = {"role": "codex", "code": "execution_error", "reroute_count": 0}
        event = health._apply_triage_verdict(
            state, "fp1", incident, {"verdict": "JUDGMENT_HARD", "owner": None}, current=5000.0,
        )
        self.assertEqual(incident["escalation_stage"], "deliberation_triggered")
        self.assertEqual(incident["triggered_at"], 5000.0)
        self.assertEqual(state["deliberation_history"], [{"role": "codex", "code": "execution_error", "triggered_at": 5000.0}])
        self.assertIn("회의해", event["message"])

    def test_process_antigravity_triage_end_to_end_with_stubbed_cli(self):
        state = {
            "incidents": {
                "fp1": {
                    "role": "codex", "code": "execution_error", "reroute_count": 0,
                    "escalation_stage": "pending_antigravity_triage", "detail": "boom",
                },
            },
            "deliberation_history": [],
        }
        with mock.patch.object(health, "_run_antigravity_triage_cli", return_value="VERDICT: OWNER_DOWN\n"):
            events = health._process_antigravity_triage(state, current=5000.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "human_notified")

    def test_process_antigravity_triage_disabled_by_flag(self):
        state = {
            "incidents": {
                "fp1": {"role": "codex", "code": "execution_error", "escalation_stage": "pending_antigravity_triage"},
            },
            "deliberation_history": [],
        }
        original = health.ANTIGRAVITY_TRIAGE_ENABLED
        health.ANTIGRAVITY_TRIAGE_ENABLED = False
        try:
            with mock.patch.object(health, "_run_antigravity_triage_cli") as cli:
                events = health._process_antigravity_triage(state, current=5000.0)
                cli.assert_not_called()
        finally:
            health.ANTIGRAVITY_TRIAGE_ENABLED = original
        self.assertEqual(events, [])
        self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "pending_antigravity_triage")

    def test_process_antigravity_triage_cli_failure_leaves_incident_pending(self):
        state = {
            "incidents": {
                "fp1": {"role": "codex", "code": "execution_error", "escalation_stage": "pending_antigravity_triage"},
            },
            "deliberation_history": [],
        }
        with mock.patch.object(health, "_run_antigravity_triage_cli", side_effect=RuntimeError("agy exit=1")):
            events = health._process_antigravity_triage(state, current=5000.0)
        self.assertEqual(events, [])
        self.assertEqual(state["incidents"]["fp1"]["escalation_stage"], "pending_antigravity_triage")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k "antigravity_triage or apply_triage_verdict" -v`
Expected: FAIL — `AttributeError: module 'health' has no attribute '_run_antigravity_triage_cli'`.

- [ ] **Step 3: Implement**

Near line 62-66 (with the other binary/flag constants), add:

```python
AGY_BIN = Path(os.environ.get("AGY_BIN", str(HOME / ".local" / "bin" / "agy")))
# Triage is read-only judgment (no repo writes), so — unlike AUTO_REPAIR_ENABLED
# — it defaults on, matching CODEX_DIAGNOSIS_ENABLED's default.
ANTIGRAVITY_TRIAGE_ENABLED = os.environ.get("RODA_GEMMA_ANTIGRAVITY_TRIAGE_ENABLED", "1") == "1"
```

Above `def poll_once`, alongside the Task 5 functions:

```python
def _run_antigravity_triage_cli(prompt: str) -> str:
    # Deliberately no --mode plan: that flag hangs headless opinion-only
    # calls on a permission prompt (see design doc "부수 성과"; the fix
    # already shipped for telegram-agent-bot.py's conversation-meeting path).
    try:
        result = subprocess.run(
            [str(AGY_BIN), "--print", prompt],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Antigravity triage 실행 실패: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Antigravity triage provider 오류(exit={result.returncode}): {result.stderr[-500:]}")
    return result.stdout


def _record_deliberation_trigger(state: dict, incident: dict, current: float, *, incident_ref: str) -> dict:
    incident["escalation_stage"] = "deliberation_triggered"
    incident["triggered_at"] = current
    state.setdefault("deliberation_history", []).append({
        "role": incident.get("role", "unknown"),
        "code": incident.get("code", "unknown"),
        "triggered_at": current,
    })
    return {
        "kind": "escalation_notice",
        "role": incident.get("role", "unknown"),
        "code": "deliberation_triggered",
        "fingerprint": f"deliberation:{incident_ref}:{int(current)}",
        "message": (
            f"[Antigravity 소집] incident={incident_ref} (role={incident.get('role')}, "
            f"code={incident.get('code')}) 판단이 어려워 다같이 회의해서 결론 내주세요."
        ),
        "detail": f"incident={incident_ref}",
    }


def _apply_triage_verdict(state: dict, fingerprint: str, incident: dict, verdict: dict, current: float) -> dict | None:
    label = verdict.get("verdict")
    if label == "OWNER_DOWN":
        incident["escalation_stage"] = "human_notified"
        incident["escalation_reason"] = "owner_down"
        incident["escalated_at"] = current
        return {
            "kind": "escalation_notice",
            "role": incident.get("role", "unknown"),
            "code": "owner_down",
            "fingerprint": f"owner-down:{fingerprint}:{int(current)}",
            "message": (
                f"[Roda 알림] incident={fingerprint}의 담당자({incident.get('routed_role')})가 "
                "다운/에러 상태로 판정되었습니다. 사람 확인이 필요합니다."
            ),
            "detail": incident.get("detail", ""),
        }
    if label == "MISROUTED" and incident.get("reroute_count", 0) < 1:
        owner = verdict.get("owner")
        incident["escalation_stage"] = "awaiting_ack"
        incident["routed_role"] = owner
        incident["routed_at"] = current
        incident["ack_deadline"] = current + ROUTING_ACK_TIMEOUT_SECONDS
        incident["reroute_count"] = incident.get("reroute_count", 0) + 1
        incident.pop("ack_task_id", None)
        incident.pop("acked_at", None)
        incident.pop("completion_deadline", None)
        incident.pop("completed_at", None)
        return {
            "kind": "escalation_notice",
            "role": incident.get("role", "unknown"),
            "code": "rerouted",
            "fingerprint": f"reroute:{fingerprint}:{int(current)}",
            "message": (
                f"[Roda 재라우팅] incident={fingerprint}의 실제 담당자는 {owner}입니다. "
                f"@{BOT_USERNAMES.get(owner, owner)} 확인 요망 — 이 인시던트의 담당자입니다."
            ),
            "detail": incident.get("detail", ""),
        }
    # JUDGMENT_HARD, or a second MISROUTED verdict (reroute cap already spent).
    return _record_deliberation_trigger(state, incident, current, incident_ref=fingerprint)


def _process_antigravity_triage(state: dict, *, current: float | None = None) -> list[dict]:
    if not ANTIGRAVITY_TRIAGE_ENABLED:
        return []
    now = current if current is not None else time.time()
    events = []
    for fingerprint, incident in list(state.get("incidents", {}).items()):
        if incident.get("escalation_stage") != "pending_antigravity_triage":
            continue
        prompt = _build_triage_prompt(incident)
        try:
            output = _run_antigravity_triage_cli(prompt)
        except RuntimeError:
            continue
        verdict = _parse_triage_verdict(output)
        event = _apply_triage_verdict(state, fingerprint, incident, verdict, now)
        if event is not None:
            events.append(event)
    return events
```

In `_process_cycle` (around line 1601-1608), the existing body starts:

```python
def _process_cycle(state: dict) -> None:
    _retry_pending_merges(state)
    alerts = poll_once(state)
    _save_state(state)
    for event in alerts:
```

Add the triage step and merge its events, right after `_save_state(state)`:

```python
def _process_cycle(state: dict) -> None:
    _retry_pending_merges(state)
    alerts = poll_once(state)
    alerts.extend(_process_antigravity_triage(state))
    _save_state(state)
    for event in alerts:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -k "antigravity_triage or apply_triage_verdict" -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/roda-telegram-health-monitor.py tests/test_roda_telegram_health_monitor.py
git commit -m "feat: apply Antigravity triage verdicts, cap reroutes at 1, trigger full deliberation"
```

---

### Task 7: Roda speech gating — only report routing-confirmed incidents

**Files:**
- Modify: `bin/roda-gemma-bot.py:212-239` (`_render_unresolved_incidents`)
- Test: `tests/test_roda_gemma_bot.py` (check whether this file exists first — if not, create it following the `importlib.util.spec_from_file_location` pattern used by `tests/test_roda_telegram_health_monitor.py:1-10`, module alias `roda_bot`)

**Interfaces:**
- Consumes: `escalation_stage` field written by Tasks 2-6.
- Produces: `_render_unresolved_incidents` unchanged signature, narrowed output.

- [ ] **Step 1: Check whether the test file exists**

Run: `ls tests/test_roda_gemma_bot.py`

If it exists, read it fully before editing and match its existing import/setup style for the new test. If it does not exist, create it with this header (mirroring `tests/test_roda_telegram_health_monitor.py:1-10`):

```python
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("roda_bot", Path(__file__).parents[1] / "bin" / "roda-gemma-bot.py")
roda_bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(roda_bot)


class RodaGemmaBotTests(unittest.TestCase):
    pass
```

- [ ] **Step 2: Write the failing test**

Add inside `class RodaGemmaBotTests`:

```python
    def test_render_unresolved_incidents_excludes_incidents_without_escalation_stage(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "health.json"
            state_file.write_text(json.dumps({
                "incidents": {
                    "routed": {
                        "incident_id": "routed", "role": "codex", "code": "execution_error",
                        "status": "open", "last_seen_at": 200, "escalation_stage": "awaiting_ack",
                    },
                    "unrouted": {
                        "incident_id": "unrouted", "role": "codex", "code": "unknown_noise",
                        "status": "open", "last_seen_at": 100, "escalation_stage": None,
                    },
                },
            }), encoding="utf-8")
            rendered = roda_bot._render_unresolved_incidents(state_file)
        self.assertIn("routed", rendered)
        self.assertNotIn("unrouted", rendered)
        self.assertIn("1건", rendered)

    def test_render_unresolved_incidents_all_filtered_reports_none(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "health.json"
            state_file.write_text(json.dumps({
                "incidents": {
                    "unrouted": {
                        "incident_id": "unrouted", "role": "codex", "code": "unknown_noise",
                        "status": "open", "last_seen_at": 100, "escalation_stage": None,
                    },
                },
            }), encoding="utf-8")
            rendered = roda_bot._render_unresolved_incidents(state_file)
        self.assertEqual(rendered, "현재 장애 원장에 미해결 사건이 없습니다.")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_roda_gemma_bot -v`
Expected: FAIL — the "unrouted" incident currently appears in the rendered output (1건 assertion sees `2건`).

- [ ] **Step 4: Implement**

In `bin/roda-gemma-bot.py`, change the `open_items` filter inside `_render_unresolved_incidents` (around line 223-226):

```python
    open_items = [
        item for item in incidents.values()
        if isinstance(item, dict)
        and item.get("status") in {"open", "reopened", "mitigated"}
        and item.get("escalation_stage") is not None
    ]
```

(Every other line of the function — sorting, the 20-item cap, the summary strings — is unchanged; only the list-comprehension filter gains the `escalation_stage` clause. "감지는 넓게, 발언은 좁게": detection stays untouched, only Roda's chat-facing report narrows to incidents that entered the routing pipeline.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_roda_gemma_bot -v`
Expected: PASS

- [ ] **Step 6: Run the full test suite for both touched files**

Run: `python3 -m unittest tests.test_roda_gemma_bot tests.test_roda_telegram_health_monitor -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add bin/roda-gemma-bot.py tests/test_roda_gemma_bot.py
git commit -m "feat: narrow Roda's incident reporting to routing-confirmed incidents"
```

---

## Post-plan verification

After all 7 tasks land, run the full repo test suite once to catch any cross-file interaction the per-task runs missed:

```bash
python3 -m unittest discover -s tests -p "test_roda*.py" -v
```

Then re-read `docs/specs/2026-08-16-roda-role-escalation-design.md`'s "다음 단계" list and confirm all four bullet points are covered:
- ✅ ack/완료 타임스탬프, 재발 카운트, 인시던트 그룹핑 필드 — Task 1 (schema), Task 2 (`related_incidents`/`reroute_count`), Task 3-4 (`acked_at`/`completed_at`), Task 6 (`deliberation_history`).
- ✅ 인시던트 타입 → 담당 에이전트 고정 매핑 — Task 2 (`_route_incident`).
- ✅ 안티그래비티 승격 로직(원인 재분류 3갈래) — Task 5-6.
- ✅ 로다의 "발언 게이팅" — Task 7.

Every code-changing task above must go through this repo's verify-task gate (`bin/verify-task-orchestrator.py`) before landing — the main session may not `Edit`/`Write` these files directly.
