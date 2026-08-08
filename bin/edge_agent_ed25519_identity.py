"""Opt-in Ed25519 agent identity using the system OpenSSL binary.

The current HMAC keyring remains the default for compatibility. This module
adds an asymmetric identity path without placing private key bytes in Python
arguments, logs, or message payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


SCHEMA = "edge_agent.ed25519_identity.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class Ed25519IdentityError(RuntimeError):
    """Identity material or an OpenSSL operation failed closed."""


def _id(name: str, value: object) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise Ed25519IdentityError(f"{name} must be a safe non-empty identifier")
    return text


def _openssl() -> str:
    return os.environ.get("EDGE_AGENT_OPENSSL_BIN", "/opt/homebrew/bin/openssl")


def _private_path(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise Ed25519IdentityError("private identity key is unavailable")
    if path.stat().st_mode & 0o077:
        raise Ed25519IdentityError("private identity key permissions are unsafe")


def _public_path(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise Ed25519IdentityError("public identity key is unavailable")


def _absolute(path: Path) -> Path:
    """Make a path absolute without resolving symlinks before validation."""
    return path.expanduser().absolute()


@dataclass(frozen=True)
class Ed25519Identity:
    agent_id: str
    key_id: str
    private_key_path: Path | None
    public_key_path: Path
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _id("agent_id", self.agent_id))
        object.__setattr__(self, "key_id", _id("key_id", self.key_id))
        public = _absolute(Path(self.public_key_path))
        _public_path(public)
        object.__setattr__(self, "public_key_path", public)
        if self.private_key_path is not None:
            private = _absolute(Path(self.private_key_path))
            _private_path(private)
            object.__setattr__(self, "private_key_path", private)
        if self.schema != SCHEMA:
            raise Ed25519IdentityError("unsupported identity schema")

    @classmethod
    def generate(cls, root: str | os.PathLike[str], *, agent_id: str, key_id: str) -> "Ed25519Identity":
        agent = _id("agent_id", agent_id)
        key = _id("key_id", key_id)
        directory = _absolute(Path(root))
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise Ed25519IdentityError("identity root is unsafe")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        if directory.is_symlink() or not directory.is_dir():
            raise Ed25519IdentityError("identity root is unsafe")
        private = directory / f"{agent}.{key}.private.pem"
        public = directory / f"{agent}.{key}.public.pem"
        if private.exists() or public.exists():
            raise Ed25519IdentityError("identity key already exists; rotation must be explicit")
        try:
            subprocess.run(
                [_openssl(), "genpkey", "-algorithm", "ED25519", "-out", str(private)],
                check=True,
                capture_output=True,
                timeout=20,
            )
            os.chmod(private, 0o600)
            subprocess.run(
                [_openssl(), "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                check=True,
                capture_output=True,
                timeout=20,
            )
            os.chmod(public, 0o600)
        except (OSError, subprocess.SubprocessError) as exc:
            for path in (private, public):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise Ed25519IdentityError("Ed25519 key generation failed") from exc
        return cls(agent, key, private, public)

    @classmethod
    def from_paths(
        cls,
        *,
        agent_id: str,
        key_id: str,
        public_key_path: str | os.PathLike[str],
        private_key_path: str | os.PathLike[str] | None = None,
    ) -> "Ed25519Identity":
        agent = _id("agent_id", agent_id)
        key = _id("key_id", key_id)
        return cls(
            agent,
            key,
            _absolute(Path(private_key_path)) if private_key_path else None,
            _absolute(Path(public_key_path)),
        )

    @property
    def has_private_key(self) -> bool:
        return self.private_key_path is not None

    def sign(self, payload: bytes) -> str:
        if self.private_key_path is None:
            raise Ed25519IdentityError("private identity key is not loaded")
        _private_path(self.private_key_path)
        with tempfile.TemporaryDirectory(prefix="edge-agent-sign-") as directory:
            root = Path(directory)
            input_path = root / "input"
            signature_path = root / "signature"
            input_path.write_bytes(bytes(payload))
            os.chmod(input_path, 0o600)
            try:
                subprocess.run(
                    [_openssl(), "pkeyutl", "-sign", "-rawin", "-inkey", str(self.private_key_path), "-in", str(input_path), "-out", str(signature_path)],
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
                return signature_path.read_bytes().hex()
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise Ed25519IdentityError("Ed25519 signing failed") from exc

    def verify(self, payload: bytes, signature: str) -> bool:
        _public_path(self.public_key_path)
        try:
            signature_bytes = bytes.fromhex(str(signature))
        except ValueError as exc:
            raise Ed25519IdentityError("Ed25519 signature is not hexadecimal") from exc
        if len(signature_bytes) != 64:
            raise Ed25519IdentityError("Ed25519 signature has invalid length")
        with tempfile.TemporaryDirectory(prefix="edge-agent-verify-") as directory:
            root = Path(directory)
            input_path = root / "input"
            signature_path = root / "signature"
            input_path.write_bytes(bytes(payload))
            signature_path.write_bytes(signature_bytes)
            os.chmod(input_path, 0o600)
            os.chmod(signature_path, 0o600)
            result = subprocess.run(
                [_openssl(), "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(self.public_key_path), "-in", str(input_path), "-sigfile", str(signature_path)],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if result.returncode == 0:
                return True
            return False


__all__ = ["SCHEMA", "Ed25519Identity", "Ed25519IdentityError"]
