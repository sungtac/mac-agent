"""Offline-safe HMAC key lifecycle for Shadow body fingerprints.

The keyring is deliberately independent from Task Identity.  A key rotation
changes only body fingerprints; root, revision, and logical event identities
remain derived from Telegram metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from typing import Callable


class ShadowKeyError(RuntimeError):
    """The configured key cannot be safely used."""


def _utc(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _secure_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ShadowKeyError("HMAC key is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ShadowKeyError("HMAC key must be a regular file")
    if info.st_uid != os.geteuid():
        raise ShadowKeyError("HMAC key owner mismatch")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ShadowKeyError("HMAC key permissions are too broad")


def _key_id(key: bytes) -> str:
    return "hmac-" + hashlib.sha256(key).hexdigest()[:16]


@dataclass(frozen=True)
class ShadowKeyMetadata:
    key_id: str
    created_at: str
    activated_at: str
    expires_at: str | None
    status: str
    algorithm: str = "HMAC-SHA256"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "key_id": self.key_id,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "algorithm": self.algorithm,
        }


class HMACKeyring:
    """Load one key at startup and reload only explicitly.

    The key bytes never leave this object and are never serialized.  Metadata
    is returned separately so event payloads can record only ``key_id``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        rotation_days: int = 90,
    ) -> None:
        self.path = Path(path).expanduser()
        self.clock = clock
        self.rotation_days = int(rotation_days)
        if self.rotation_days <= 0:
            raise ValueError("rotation_days must be positive")
        self._key: bytes | None = None
        self._metadata: ShadowKeyMetadata | None = None
        self.reload()

    @property
    def available(self) -> bool:
        return self._key is not None and self._metadata is not None

    @property
    def key(self) -> bytes | None:
        return self._key

    @property
    def key_id(self) -> str | None:
        return self._metadata.key_id if self._metadata else None

    @property
    def metadata(self) -> dict[str, str | None] | None:
        return self._metadata.as_dict() if self._metadata else None

    def reload(self) -> ShadowKeyMetadata:
        _secure_file(self.path)
        key = self.path.read_bytes()
        if len(key) < 16:
            raise ShadowKeyError("HMAC key is too short")
        now = _utc(self.clock())
        metadata_path = self.path.with_name(self.path.name + ".metadata.json")
        metadata: dict[str, str | None] = {}
        if metadata_path.exists():
            _secure_file(metadata_path)
            try:
                loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    metadata = loaded
            except (OSError, ValueError) as exc:
                raise ShadowKeyError("HMAC key metadata is invalid") from exc
        computed_key_id = _key_id(key)
        key_id = str(metadata.get("key_id") or computed_key_id)
        if key_id != computed_key_id:
            raise ShadowKeyError("HMAC key metadata does not match key material")
        self._key = key
        self._metadata = ShadowKeyMetadata(
            key_id=key_id,
            created_at=str(metadata.get("created_at") or now),
            activated_at=str(metadata.get("activated_at") or now),
            expires_at=metadata.get("expires_at"),
            status=str(metadata.get("status") or "active"),
        )
        return self._metadata

    def fingerprint(self, value: str | bytes | None) -> dict[str, str | None]:
        if not self.available or value is None:
            return {"body_hash": None, "body_hash_status": "UNKNOWN", "body_hmac_key_id": None}
        payload = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        return {
            "body_hash": hmac.new(self._key, payload, hashlib.sha256).hexdigest(),
            "body_hash_status": "HMAC-SHA256",
            "body_hmac_key_id": self.key_id,
        }

    def rotate(self, key: bytes | None = None, *, now: float | None = None) -> ShadowKeyMetadata:
        """Atomically replace the test/offline key and explicitly reload it."""

        value = bytes(key or secrets.token_bytes(32))
        if len(value) < 16:
            raise ShadowKeyError("replacement HMAC key is too short")
        timestamp = float(self.clock() if now is None else now)
        created = _utc(timestamp)
        expires = _utc(timestamp + self.rotation_days * 86400)
        metadata = ShadowKeyMetadata(_key_id(value), created, created, expires, "active")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            metadata_path = self.path.with_name(self.path.name + ".metadata.json")
            fd, temp_meta = tempfile.mkstemp(prefix=f".{metadata_path.name}.", dir=self.path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(metadata.as_dict(), handle, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_meta, metadata_path)
            finally:
                if os.path.exists(temp_meta):
                    os.unlink(temp_meta)
            self.reload()
            return metadata
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def create_test_key(path: str | Path, *, value: bytes | None = None) -> Path:
    """Create a private test key only; never called by production startup."""

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(value or secrets.token_bytes(32))
    os.chmod(target, 0o600)
    return target


__all__ = ["HMACKeyring", "ShadowKeyError", "ShadowKeyMetadata", "create_test_key"]
