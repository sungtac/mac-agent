#!/usr/bin/env python3
"""Opt-in terminal provider runner backed by the canonical engine dispatcher.

The shell provider entrypoint remains the compatibility boundary.  When its
Canary flag is enabled, this runner projects the rendered terminal prompt into
the engine's TerminalAdapter and waits only for its own result; unrelated core
tasks remain independently schedulable.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys


ENGINE_REPO = Path(
    os.environ.get(
        "MULTIAGENT_ENGINE_REPO",
        "/Users/edge_ai/tools/multi-agent-starter/engine-repo",
    )
).expanduser().resolve()
if str(ENGINE_REPO) not in sys.path:
    sys.path.insert(0, str(ENGINE_REPO))

from orchestrator.engine import MultiAgentEngine  # noqa: E402
from state.usage import UsageLedger  # noqa: E402
from terminal.adapter import TerminalAdapter  # noqa: E402


@dataclass(frozen=True)
class ProviderResult:
    text: str
    usage_tokens: int | None = None
    usage_status: str = "unknown"


def _provider_command(provider: str, prompt: str, workdir: str) -> tuple[list[str], dict[str, str]]:
    sandbox = Path(
        os.environ.get(
            "EDGE_AGENT_PROVIDER_SANDBOX",
            "/Users/edge_ai/mac-agent/bin/edge-agent-provider-sandbox.sh",
        )
    ).expanduser()
    if provider == "claude":
        cli = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
        return [str(sandbox), cli, "-p", prompt], dict(os.environ)
    if provider == "codex":
        cli = os.environ.get("CODEX_BIN", "/opt/homebrew/bin/codex")
        return [str(sandbox), cli, "exec", "-s", "workspace-write", "-C", workdir, prompt], dict(os.environ)
    if provider == "agy":
        cli = os.environ.get("AGY_BIN", str(Path.home() / ".local/bin/agy"))
        environment = dict(os.environ)
        for key in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT"):
            environment.pop(key, None)
        return [str(sandbox), cli, "--print", prompt], environment
    raise ValueError(f"unsupported provider: {provider}")


def run_provider(provider: str, prompt: str, workdir: str) -> ProviderResult:
    command, environment = _provider_command(provider, prompt, workdir)
    timeout = max(30, int(os.environ.get("MULTIAGENT_ENGINE_TERMINAL_TIMEOUT_SECONDS", "1800")))
    completed = subprocess.run(
        command,
        cwd=workdir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    if completed.returncode != 0:
        raise RuntimeError(f"{provider} exited with status {completed.returncode}")
    return ProviderResult(completed.stdout, usage_status="unknown")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=("claude", "codex", "agy"))
    parser.add_argument("prompt_file")
    parser.add_argument("workdir")
    args = parser.parse_args()
    prompt_path = Path(args.prompt_file).expanduser().resolve()
    workdir = Path(args.workdir).expanduser().resolve()
    prompt = prompt_path.read_text(encoding="utf-8")
    task_id = os.environ.get("EDGE_AGENT_TASK_ID", "").strip() or f"terminal-{prompt_path.stem}"
    source_event_id = os.environ.get("EDGE_AGENT_SOURCE_EVENT_ID", "").strip() or task_id
    engine_home = Path(
        os.environ.get("MULTIAGENT_ENGINE_HOME", str(Path.home() / "Library/Application Support/multiagent-engine"))
    ).expanduser()
    engine = MultiAgentEngine(state_root=engine_home, target_root=workdir)
    try:
        adapter = TerminalAdapter(engine, workspace=workdir, provider=args.provider)
        ledger = UsageLedger(engine_home / "usage")
        submission = adapter.submit_provider(
            task_id=task_id,
            prompt=prompt,
            source_event_id=source_event_id,
            handler=lambda rendered_prompt: run_provider(args.provider, rendered_prompt, str(workdir)),
            usage_ledger=ledger,
            resource_key=f"worktree:{workdir}" if args.provider == "codex" else "",
        )
        if submission.receipt.status != "accepted" or submission.receipt.future is None:
            print(
                f"terminal dispatch not accepted: {submission.receipt.status}",
                file=sys.stderr,
            )
            return 75
        result, _usage = submission.receipt.future.result()
        print(result.text, end="")
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
