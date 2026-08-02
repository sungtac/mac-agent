"""One repository-keyed lock implementation for every Edge Agent adapter."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import time
from pathlib import Path
from typing import Iterator

from edge_agent_locks import canonical_repository_root
from edge_agent_secure_paths import ensure_private_directory, open_lock


REPO_LOCK_DIR = Path.home() / ".claude" / "discord-bot" / "repo-locks"


class RepoLockBusy(RuntimeError):
    """Another process currently owns the canonical repository lock."""


def repo_lock_path(resolved_path: str | Path) -> Path:
    canonical = str(canonical_repository_root(resolved_path))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return REPO_LOCK_DIR / f"{digest}.lock"


@contextlib.contextmanager
def try_acquire_repo_lock(resolved_path: str | Path) -> Iterator[None]:
    ensure_private_directory(REPO_LOCK_DIR)
    fd = open_lock(repo_lock_path(resolved_path))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RepoLockBusy(str(resolved_path)) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextlib.asynccontextmanager
async def acquire_repo_lock(resolved_path: str | Path, *, wait_seconds: int = 1800, on_wait=None):
    """Wait for the same lock used by synchronous terminal/Discord callers."""
    ensure_private_directory(REPO_LOCK_DIR)
    fd = open_lock(repo_lock_path(resolved_path))
    acquired = False
    try:
        deadline = time.monotonic() + max(1, int(wait_seconds))
        while not acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RepoLockBusy(str(resolved_path))
                if on_wait is not None:
                    await on_wait()
                await asyncio.sleep(2)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
