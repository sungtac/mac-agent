"""Shared lock identity helpers for Edge Agent processes."""

from __future__ import annotations

import subprocess
from pathlib import Path


def canonical_repository_root(path: str | Path) -> Path:
    """Return the common repository root shared by normal checkouts/worktrees.

    A non-git path falls back to its resolved path so the current workspace
    behavior remains unchanged. This function only reads git metadata.
    """
    resolved = Path(path).expanduser().resolve()
    try:
        top = Path(
            subprocess.check_output(
                ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        common = Path(
            subprocess.check_output(
                ["git", "-C", str(resolved), "rev-parse", "--git-common-dir"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        if not common.is_absolute():
            # Git emits this relative to the directory passed with -C,
            # especially when the caller is below the repository root.
            common = resolved / common
        common = common.resolve()
        if common.name == ".git":
            return common.parent
        return top.resolve()
    except (OSError, subprocess.CalledProcessError, ValueError):
        return resolved
