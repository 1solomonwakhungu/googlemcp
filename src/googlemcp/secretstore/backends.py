"""Local secret backends: OS keyring, 0600 JSON file, and read-only env vars."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from keyring.errors import KeyringError

from googlemcp.secretstore.base import SecretKey, SecretStoreError, ensure_writable


class _KeyringAPI(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class KeyringStore:
    """OS keyring (macOS Keychain, Windows Credential Locker, Secret Service).

    Entries use service ``<prefix>`` and the logical key as the username.
    """

    name = "keyring"
    read_only = False

    def __init__(self, prefix: str = "googlemcp", backend: _KeyringAPI | None = None):
        self._service = prefix
        self._backend = backend

    def _api(self) -> _KeyringAPI:
        if self._backend is not None:
            return self._backend
        import keyring

        return keyring

    def get(self, key: SecretKey) -> str | None:
        try:
            return self._api().get_password(self._service, key.value)
        except (KeyringError, RuntimeError, OSError) as exc:
            raise SecretStoreError(_keyring_failure("read", key, exc)) from None

    def set(self, key: SecretKey, value: str) -> None:
        try:
            self._api().set_password(self._service, key.value, value)
        except (KeyringError, RuntimeError, OSError) as exc:
            raise SecretStoreError(_keyring_failure("write", key, exc)) from None

    def delete(self, key: SecretKey) -> None:
        if self.get(key) is None:
            return
        try:
            self._api().delete_password(self._service, key.value)
        except (KeyringError, RuntimeError, OSError) as exc:
            raise SecretStoreError(_keyring_failure("delete", key, exc)) from None


def _keyring_failure(action: str, key: SecretKey, exc: Exception) -> str:
    # Exception text from keyring backends does not include the stored value,
    # but only the type name is reported to stay on the safe side.
    return (
        f"keyring could not {action} {key.value!r} ({type(exc).__name__}). "
        "Install a keyring backend or set GOOGLEMCP_SECRET_BACKEND=file for local development."
    )


class FileStore:
    """JSON file readable only by the owner. For development and headless hosts.

    The file and its directory are created with modes 0600 and 0700. Any
    existing file that grants group or other permissions is refused rather
    than silently fixed, because its contents may already have leaked.
    """

    name = "file"
    read_only = False

    def __init__(self, path: Path):
        self.path = path

    def _check_mode(self) -> None:
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise SecretStoreError(
                f"refusing to use {self.path}: permissions {mode:04o} allow group or other "
                f"access. Rotate the secrets it holds, then run: chmod 600 {self.path}"
            )

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        self._check_mode()
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SecretStoreError(
                f"cannot parse secret file {self.path} ({type(exc).__name__})"
            ) from None
        if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
            raise SecretStoreError(f"secret file {self.path} must be a JSON object of strings")
        return data

    def _save(self, data: Mapping[str, str]) -> None:
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".secrets-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(data), handle, indent=2, sort_keys=True)
                handle.write("\n")
            Path(tmp).replace(self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                Path(tmp).unlink()
            raise

    def get(self, key: SecretKey) -> str | None:
        return self._load().get(key.value)

    def set(self, key: SecretKey, value: str) -> None:
        data = self._load()
        data[key.value] = value
        self._save(data)

    def delete(self, key: SecretKey) -> None:
        data = self._load()
        if data.pop(key.value, None) is not None:
            self._save(data)


class DirStore:
    """Read-only secrets stored as files, one per key, in a local directory.

    A key is read from ``<dir>/<key>`` when that is a file, or from
    ``<dir>/<key>/value`` when each secret has its own directory (the layout
    some secret managers and sync tools write). Files are read on every
    access, so a re-issued refresh token is picked up without a restart.
    """

    name = "dir"
    read_only = True
    ITEM = "value"

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, key: SecretKey) -> Path:
        base = self.directory / key.value
        return base / self.ITEM if base.is_dir() else base

    def get(self, key: SecretKey) -> str | None:
        path = self._path(key)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SecretStoreError(
                f"cannot read mounted secret {key.value!r} ({type(exc).__name__})"
            ) from None
        return value or None

    def set(self, key: SecretKey, value: str) -> None:
        ensure_writable(self, key)

    def delete(self, key: SecretKey) -> None:
        ensure_writable(self, key)


class EnvStore:
    """Read-only secrets from ``GOOGLEMCP_<KEY>`` environment variables.

    Suited to CI and containers whose platform injects secrets as env vars.
    Rotated refresh tokens cannot be persisted from this backend.
    """

    name = "env"
    read_only = True

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def get(self, key: SecretKey) -> str | None:
        return self._environ.get(key.env_var) or None

    def set(self, key: SecretKey, value: str) -> None:
        ensure_writable(self, key)

    def delete(self, key: SecretKey) -> None:
        ensure_writable(self, key)
