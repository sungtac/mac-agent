#!/usr/bin/env python3
"""Read-only capability discovery for the provider-neutral Edge Agent.

This module answers "what is available in this environment?" without
printing command output that could contain credentials or private tokens. It
does not grant authorization and never changes files, services, accounts, or
network configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CapabilityObservation:
    capability: str
    state: str
    evidence: str
    authorization: str = "not_determined"


def _command_exists(names: tuple[str, ...]) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _run_quiet(argv: list[str], *, cwd: Path | None = None, timeout: float = 5) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, type(exc).__name__
    # The output is intentionally discarded from the rendered result. Only a
    # bounded, non-sensitive classification is returned to the provider.
    return completed.returncode, completed.stdout + completed.stderr


def _command_observation(capability: str, names: tuple[str, ...]) -> CapabilityObservation:
    path = _command_exists(names)
    if not path:
        return CapabilityObservation(capability, "unavailable", "executable not found")
    return CapabilityObservation(capability, "available", "executable present", "not_determined")


def _git_observations(workdir: Path | None) -> list[CapabilityObservation]:
    if workdir is None:
        return [CapabilityObservation("workspace", "unknown", "no workdir supplied")]
    if not workdir.is_dir():
        return [CapabilityObservation("workspace", "unavailable", "workdir does not exist")]
    rc, _ = _run_quiet(["/usr/bin/git", "-C", str(workdir), "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return [CapabilityObservation("workspace", "unavailable", "not a Git worktree")]
    status_rc, status = _run_quiet(
        ["/usr/bin/git", "-C", str(workdir), "status", "--porcelain", "--untracked-files=all"]
    )
    remote_rc, _ = _run_quiet(["/usr/bin/git", "-C", str(workdir), "remote", "get-url", "origin"])
    dirty = bool(status.strip()) if status_rc == 0 else None
    evidence = "Git worktree; "
    if dirty is True:
        evidence += "dirty changes observed; preserve them"
    elif dirty is False:
        evidence += "clean"
    else:
        evidence += "status unknown"
    return [
        CapabilityObservation("workspace", "available", evidence),
        CapabilityObservation(
            "repository_remote",
            "available" if remote_rc == 0 else "unknown",
            "origin configured" if remote_rc == 0 else "origin not confirmed",
        ),
    ]


def _github_observation() -> CapabilityObservation:
    if not shutil.which("gh"):
        return CapabilityObservation("github_cli", "unavailable", "gh executable not found")
    rc, _ = _run_quiet(["gh", "auth", "status", "--hostname", "github.com"], timeout=8)
    if rc == 0:
        return CapabilityObservation(
            "github_cli", "available", "gh is authenticated; command output suppressed", "not_determined"
        )
    return CapabilityObservation(
        "github_cli", "unknown", "gh is installed but authentication was not confirmed", "not_determined"
    )


def _tailscale_observation() -> CapabilityObservation:
    if not shutil.which("tailscale"):
        return CapabilityObservation("tailscale", "unavailable", "tailscale executable not found")
    rc, output = _run_quiet(["tailscale", "status", "--json"], timeout=8)
    if rc != 0:
        return CapabilityObservation("tailscale", "unknown", "tailscale installed; status not confirmed")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        # Output is truncated and deliberately not exposed; successful exit is
        # enough to say the daemon responded, not that Funnel is configured.
        payload = {}
    online = payload.get("Self", {}).get("Online") if isinstance(payload, dict) else None
    return CapabilityObservation(
        "tailscale",
        "available" if online is True else "unknown",
        "daemon online" if online is True else "daemon responded; online state not confirmed",
    )


def _tailscale_funnel_observation() -> CapabilityObservation:
    if not shutil.which("tailscale"):
        return CapabilityObservation("tailscale_funnel", "unavailable", "tailscale executable not found")
    rc, output = _run_quiet(["tailscale", "funnel", "status"], timeout=8)
    if rc != 0:
        return CapabilityObservation(
            "tailscale_funnel", "unknown", "tailscale installed; public Funnel status not confirmed"
        )
    lowered = output.casefold()
    if "funnel on" in lowered or "https://" in lowered:
        return CapabilityObservation("tailscale_funnel", "available", "public Funnel configuration observed")
    return CapabilityObservation(
        "tailscale_funnel", "unknown", "tailscale responded; public Funnel not confirmed"
    )


def collect(workdir: str | os.PathLike[str] | None = None) -> tuple[CapabilityObservation, ...]:
    resolved_workdir = Path(workdir).expanduser().resolve() if workdir else None
    observations = [
        _command_observation("claude_provider", ("claude",)),
        _command_observation("codex_provider", ("codex",)),
        _command_observation("antigravity_provider", ("agy",)),
        _github_observation(),
        _tailscale_observation(),
        _tailscale_funnel_observation(),
    ]
    observations.extend(_git_observations(resolved_workdir))
    return tuple(observations)


def render_prompt(workdir: str | os.PathLike[str] | None = None) -> str:
    observations = collect(workdir)
    lines = [
        "[Capability-first preflight: read-only observations]",
        "Treat unavailable as a conclusion only when the observation says unavailable; unknown means verify further.",
    ]
    lines.extend(f"- {item.capability}: {item.state} ({item.evidence})" for item in observations)
    lines.append(
        "These observations do not grant authorization. Still check task scope, user approval, provider cost, "
        "secrets, destructive effects, and external communication before acting."
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Edge Agent capability preflight")
    parser.add_argument("--workdir", default="", help="optional worktree to inspect")
    parser.add_argument("--format", choices=("json", "prompt"), default="json")
    args = parser.parse_args()
    observations = collect(args.workdir or None)
    if args.format == "prompt":
        print(render_prompt(args.workdir or None))
    else:
        print(json.dumps([asdict(item) for item in observations], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
