"""Encrypted-at-rest secret store (DB passwords, DO/SMTP/Slack/SSH credentials).

Secrets never touch the SQLite DB or any run artifact. They live Fernet-encrypted
in ``secrets.enc`` (0600), keyed by a 0600 ``secret.key`` under the data dir; the
DB only ever stores an opaque *reference name*. Plaintext exists only in memory
and is injected into the child process environment at exec time, exactly like the
harness's existing ``PGPASSWORD`` handling. Losing ``secret.key`` means stored
secrets can't be decrypted (documented in OPERATIONS.md backup section).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


def _load_key(key_path: Path) -> bytes:
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Write 0600 from the start (umask-safe): open with restrictive mode.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class SecretStore:
    """Reference-keyed encrypted store. The DB holds only the reference names."""

    def __init__(self, key_path: Path, store_path: Path) -> None:
        self._fernet = Fernet(_load_key(key_path))
        self._path = store_path

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return dict(json.loads(self._path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return {}

    def _write(self, data: dict[str, str]) -> None:
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self._path)

    def set(self, ref: str, value: str) -> None:
        data = self._read()
        data[ref] = self._fernet.encrypt(value.encode()).decode()
        self._write(data)

    def get(self, ref: str) -> Optional[str]:
        token = self._read().get(ref)
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

    def delete(self, ref: str) -> None:
        data = self._read()
        if data.pop(ref, None) is not None:
            self._write(data)

    def refs(self) -> list[str]:
        return sorted(self._read())
