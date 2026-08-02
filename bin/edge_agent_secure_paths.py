"""Small no-follow filesystem primitives for private Edge Agent state."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TextIO


class SecurePathError(RuntimeError):
    """A managed state path contains a symlink or an unexpected file type."""


def _absolute(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = Path.cwd() / value
    return Path(os.path.abspath(str(value)))


def reject_symlink_components(path: str | Path) -> Path:
    """Return an absolute path after rejecting the managed path component.

    macOS exposes ``/var`` as a system symlink to ``/private/var``. Managed
    roots are checked as leaf directories/files, while their OS-owned parent
    aliases are intentionally allowed. Callers validate each managed parent
    with :func:`ensure_private_directory` before creating children.
    """
    value = _absolute(path)
    try:
        mode = os.lstat(value).st_mode
    except FileNotFoundError:
        return value
    if stat.S_ISLNK(mode):
        raise SecurePathError(f"managed path contains a symlink: {value}")
    return value


def ensure_private_directory(path: str | Path) -> Path:
    value = reject_symlink_components(path)
    value.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = reject_symlink_components(value)
    try:
        mode = os.lstat(value).st_mode
    except FileNotFoundError as exc:
        raise SecurePathError(f"managed directory was not created: {value}") from exc
    if not stat.S_ISDIR(mode):
        raise SecurePathError(f"managed path is not a directory: {value}")
    os.chmod(value, 0o700)
    return value


def open_lock(path: str | Path) -> int:
    value = reject_symlink_components(path)
    ensure_private_directory(value.parent)
    try:
        return os.open(
            str(value),
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise SecurePathError(f"managed lock cannot be opened safely: {value}") from exc


def read_text(path: str | Path, *, encoding: str = "utf-8") -> str:
    value = reject_symlink_components(path)
    try:
        descriptor = os.open(str(value), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SecurePathError(f"managed file cannot be read safely: {value}") from exc
    try:
        with os.fdopen(descriptor, "r", encoding=encoding) as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def append_text(path: str | Path, *, encoding: str = "utf-8") -> TextIO:
    value = reject_symlink_components(path)
    ensure_private_directory(value.parent)
    try:
        descriptor = os.open(
            str(value),
            os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise SecurePathError(f"managed file cannot be appended safely: {value}") from exc
    return os.fdopen(descriptor, "a", encoding=encoding)
