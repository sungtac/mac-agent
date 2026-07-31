#!/usr/bin/env python3
"""Deterministic protected-path policy for the Edge Agent workspace.

The default CLI mode is dry-run. It classifies paths only; it never creates,
modifies, deletes, or moves files. Runtime callers can adopt the same contract
later after the impact has been reviewed.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "config" / "edge-agent-boundary.json"


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class PathDecision:
    path: str
    classification: str
    allowed_by_policy: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "classification": self.classification,
            "allowed_by_policy": self.allowed_by_policy,
            "reason": self.reason,
        }


class EdgeAgentWritePolicy:
    def __init__(self, manifest_path: str | Path = DEFAULT_MANIFEST):
        manifest = json.loads(_resolve(manifest_path).read_text(encoding="utf-8"))
        self.mode = str(manifest.get("mode", "unknown"))
        self.legacy_workspace = _resolve(manifest["legacy_shared_workspace"])
        self.protected_roots = tuple(_resolve(item) for item in manifest.get("protected_roots", []))

    def classify(self, path: str | Path) -> PathDecision:
        resolved = _resolve(path)
        for root in self.protected_roots:
            if _is_within(resolved, root):
                return PathDecision(
                    path=str(resolved),
                    classification="protected",
                    allowed_by_policy=False,
                    reason=f"path is inside protected root: {root.name}",
                )
        if _is_within(resolved, self.legacy_workspace):
            return PathDecision(
                path=str(resolved),
                classification="legacy_workspace_allowed",
                allowed_by_policy=True,
                reason="path is inside the legacy workspace but outside protected roots",
            )
        return PathDecision(
            path=str(resolved),
            classification="outside_legacy_workspace",
            allowed_by_policy=False,
            reason="path is outside the declared legacy workspace; explicit boundary decision required",
        )

    def check(self, paths: list[str | Path]) -> list[PathDecision]:
        return [self.classify(path) for path in paths]


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify Edge Agent write paths without changing anything")
    parser.add_argument("paths", nargs="+", help="paths to classify")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="return 1 if any path is not allowed")
    args = parser.parse_args()
    try:
        policy = EdgeAgentWritePolicy(args.manifest)
        decisions = policy.check(args.paths)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    payload = {"mode": policy.mode, "decisions": [item.to_dict() for item in decisions]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in decisions:
            print(f"{item.classification}: {item.path} — {item.reason}")
    if args.strict and any(not item.allowed_by_policy for item in decisions):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
